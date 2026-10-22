"""
sec_companies.py
────────────────
Listado oficial de empresas cotizadas de la SEC (CIK, ticker y nombre),
para el buscador de la app. Viene ordenado por tamaño, así que las
empresas grandes salen primero. Se guarda en memoria y se renueva cada día.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
REFRESH_EVERY = timedelta(hours=24)


@dataclass
class ListedCompany:
    cik: str            # con ceros a la izquierda, como en la tabla companies
    ticker: str         # ticker principal (el primero que da la SEC)
    name: str
    rank: int           # posición en el listado de la SEC (0 = la más grande)
    tickers: list[str] = field(default_factory=list)  # todos sus tickers (GOOGL, GOOG…)


_companies: list[ListedCompany] = []
_by_cik: dict[str, ListedCompany] = {}
_loaded_at: datetime | None = None
_lock = asyncio.Lock()


async def listed_companies() -> list[ListedCompany]:
    """Empresas cotizadas, de la más grande a la más pequeña. Si la SEC falla, usa la copia anterior."""
    global _companies, _by_cik, _loaded_at
    async with _lock:
        if _loaded_at and datetime.utcnow() - _loaded_at < REFRESH_EVERY:
            return _companies
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    SEC_TICKERS_URL, headers={"User-Agent": settings.EDGAR_USER_AGENT}
                )
                response.raise_for_status()
            companies: list[ListedCompany] = []
            by_cik: dict[str, ListedCompany] = {}
            for row in response.json().values():
                cik = str(row["cik_str"]).zfill(10)
                ticker = row["ticker"].upper()
                if cik in by_cik:  # mismo emisor con otra clase de acción
                    by_cik[cik].tickers.append(ticker)
                    continue
                company = ListedCompany(cik, ticker, row["title"], len(companies), [ticker])
                companies.append(company)
                by_cik[cik] = company
            _companies, _by_cik, _loaded_at = companies, by_cik, datetime.utcnow()
            logger.info(f"Loaded {len(companies)} listed companies from the SEC")
        except Exception as e:
            logger.error(f"SEC company list failed: {e}")
        return _companies


async def find_by_cik(cik: str) -> ListedCompany | None:
    await listed_companies()
    return _by_cik.get(cik.zfill(10))


async def search(query: str, limit: int = 20) -> list[ListedCompany]:
    """
    Busca por ticker o nombre. Primero el ticker exacto, luego tickers que
    empiezan por la búsqueda, luego nombres; a igualdad, la empresa más grande.
    """
    q = query.strip().upper()
    if not q:
        return []
    matches: list[tuple[int, int, ListedCompany]] = []
    for c in await listed_companies():
        name = c.name.upper()
        if q in c.tickers:
            level = 0
        elif any(t.startswith(q) for t in c.tickers):
            level = 1
        elif name.startswith(q) or f" {q}" in name:
            level = 2
        elif q in name:
            level = 3
        else:
            continue
        matches.append((level, c.rank, c))
    matches.sort(key=lambda m: (m[0], m[1]))
    return [c for _, _, c in matches[:limit]]
