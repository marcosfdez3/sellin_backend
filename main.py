"""
main.py
───────
Punto de entrada de la API. Arranca FastAPI, inicializa la BD
y programa el job de sync con EDGAR.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import settings
from app.core.database import init_db, AsyncSessionLocal
from app.api.signals import router as signals_router
from app.api.transactions import transactions_router, companies_router
from app.services.sync_service import SyncService
from app.schemas.schemas import SyncStatsOut

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def run_sync_job():
    """Job periódico que EDGAR corre en background."""
    logger.info("Starting scheduled EDGAR sync...")
    async with AsyncSessionLocal() as db:
        service = SyncService()
        stats = await service.sync_recent_filings(db, count=100)
        logger.info(f"Sync done: {stats}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing database...")
    await init_db()

    logger.info(f"Scheduling EDGAR sync every {settings.SYNC_INTERVAL_MINUTES} minutes")
    scheduler.add_job(
        run_sync_job,
        "interval",
        minutes=settings.SYNC_INTERVAL_MINUTES,
        id="edgar_sync",
        replace_existing=True,
    )
    scheduler.start()

    # Sync inicial al arrancar
    await run_sync_job()

    yield

    # Shutdown
    scheduler.shutdown()


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    description="API para tracking de transacciones de insiders de la SEC",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En prod: especificar tu dominio
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Routers ───────────────────────────────────────────────────────────────────
app.include_router(signals_router, prefix="/api/v1")
app.include_router(transactions_router, prefix="/api/v1")
app.include_router(companies_router, prefix="/api/v1")


# ─── Endpoints de utilidad ─────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@app.post("/api/v1/admin/sync", response_model=SyncStatsOut)
async def trigger_sync():
    """Dispara un sync manual. Útil durante desarrollo."""
    async with AsyncSessionLocal() as db:
        service = SyncService()
        stats = await service.sync_recent_filings(db, count=100)
        return SyncStatsOut(**stats)


@app.get("/api/v1/admin/debug-filing")
async def debug_filing():
    """
    Descarga UN solo Form 4 y devuelve el detalle de cada paso.
    Úsalo para diagnosticar errores del pipeline.
    """
    import traceback
    from app.services.edgar_fetcher import EdgarFetcher
    from app.services.form4_parser import Form4Parser

    result = {"steps": {}}

    async with EdgarFetcher() as fetcher:
        # Paso 1: obtener lista de filings
        try:
            filings = await fetcher.get_recent_form4_filings(count=3)
            result["steps"]["1_fetch_list"] = {
                "ok": True,
                "count": len(filings),
                "sample": filings[0] if filings else None,
            }
        except Exception as e:
            result["steps"]["1_fetch_list"] = {"ok": False, "error": str(e)}
            return result

        if not filings:
            result["steps"]["1_fetch_list"]["note"] = "No filings returned"
            return result

        meta = filings[0]

        # Paso 2: descargar el XML
        try:
            xml_result = await fetcher.get_form4_xml(meta["index_url"])
            if xml_result:
                xml_content, accession = xml_result
                result["steps"]["2_fetch_xml"] = {
                    "ok": True,
                    "accession": accession,
                    "xml_length": len(xml_content),
                    "xml_preview": xml_content[:500],
                }
            else:
                result["steps"]["2_fetch_xml"] = {
                    "ok": False,
                    "error": "get_form4_xml returned None",
                    "index_url": meta["index_url"],
                }
                return result
        except Exception as e:
            result["steps"]["2_fetch_xml"] = {
                "ok": False,
                "error": str(e),
                "traceback": traceback.format_exc(),
            }
            return result

        # Paso 3: parsear el XML
        try:
            parser = Form4Parser()
            parsed = parser.parse(
                xml_content=xml_content,
                accession_number=accession,
                filed_date=meta.get("filed_date", ""),
                filing_url=meta["index_url"],
            )
            if parsed:
                result["steps"]["3_parse_xml"] = {
                    "ok": True,
                    "company": parsed.company_name,
                    "ticker": parsed.company_ticker,
                    "insiders": [i.name for i in parsed.insiders],
                    "transactions": len(parsed.transactions),
                    "has_buys": parsed.has_open_market_buys,
                    "has_sells": parsed.has_open_market_sells,
                    "total_buy_value": parsed.total_buy_value,
                }
            else:
                result["steps"]["3_parse_xml"] = {
                    "ok": False,
                    "error": "parser.parse() returned None",
                }
        except Exception as e:
            result["steps"]["3_parse_xml"] = {
                "ok": False,
                "error": str(e),
                "traceback": traceback.format_exc(),
            }

    return result


@app.get("/api/v1/admin/debug-index")
async def debug_index():
    """Muestra todos los links encontrados en la página de índice de un filing."""
    from lxml import etree
    from app.services.edgar_fetcher import EdgarFetcher
    import re

    async with EdgarFetcher() as fetcher:
        filings = await fetcher.get_recent_form4_filings(count=3)
        if not filings:
            return {"error": "No filings found"}

        meta = filings[0]
        index_url = meta["index_url"]

        response = await fetcher._get(index_url)
        html = response.text

        # Extraer todos los links con sus textos y celdas vecinas
        root = etree.fromstring(html.encode(), etree.HTMLParser())
        rows = root.findall(".//tr")

        table_data = []
        for row in rows:
            cells = [td.text_content() if hasattr(td, 'text_content') else "".join(td.itertext()) for td in row.findall(".//td")]
            links = [a.get("href", "") for a in row.findall(".//a")]
            if links:
                table_data.append({"cells": cells, "links": links})

        # También extraer todos los .xml directamente
        all_xml_links = [
            a.get("href", "")
            for a in root.findall(".//a")
            if a.get("href", "").endswith(".xml")
        ]

        return {
            "index_url": index_url,
            "company": meta["company_name"],
            "all_xml_links": all_xml_links,
            "table_rows_with_links": table_data[:20],
            "html_preview": html[:1000],
        }