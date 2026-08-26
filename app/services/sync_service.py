"""
sync_service.py
───────────────
Orquesta el pipeline completo de ingesta de Form 4s.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Company, Insider, Transaction, TransactionType, InsiderRole
from app.services.edgar_fetcher import EdgarFetcher
from app.services.form4_parser import Form4Parser, ParsedForm4, ParsedInsider
from app.services.signal_engine import SignalEngine

logger = logging.getLogger(__name__)


class SyncService:

    def __init__(self):
        self.parser = Form4Parser()
        self.signal_engine = SignalEngine()

    async def sync_recent_filings(self, db: AsyncSession, count: int = 40) -> dict:
        stats = {
            "filings_fetched": 0,
            "filings_saved": 0,
            "filings_skipped": 0,
            "signals_generated": 0,
            "errors": 0,
        }

        async with EdgarFetcher() as fetcher:
            filings_meta = await fetcher.get_recent_form4_filings(count=count)
            stats["filings_fetched"] = len(filings_meta)

            for meta in filings_meta:
                try:
                    result = await fetcher.get_form4_xml(meta["index_url"])
                    if not result:
                        stats["filings_skipped"] += 1
                        continue

                    xml_content, accession = result

                    if not accession:
                        stats["filings_skipped"] += 1
                        continue

                    # Saltar si ya existe este accession number
                    if await self._filing_exists(db, accession):
                        stats["filings_skipped"] += 1
                        continue

                    parsed = self.parser.parse(
                        xml_content=xml_content,
                        accession_number=accession,
                        filed_date=meta.get("filed_date", ""),
                        filing_url=meta["index_url"],
                    )

                    if not parsed:
                        stats["filings_skipped"] += 1
                        continue

                    saved = await self._save_filing(db, parsed)
                    if saved:
                        stats["filings_saved"] += 1

                except Exception as e:
                    # CRÍTICO: hacer rollback para que la sesión pueda continuar
                    await db.rollback()
                    logger.error(f"Error processing {meta.get('index_url')}: {e}")
                    stats["errors"] += 1

        # Generar señales
        try:
            signals_count = await self._refresh_signals(db)
            stats["signals_generated"] = signals_count
        except Exception as e:
            await db.rollback()
            logger.error(f"Error generating signals: {e}")

        logger.info(f"Sync complete: {stats}")
        # Generar señales
        try:
            signals_count = await self._refresh_signals(db)
            stats["signals_generated"] = signals_count
        except Exception as e:
            await db.rollback()
            logger.error(f"Error generating signals: {e}")

        await db.commit()  # ← añade esta línea
        logger.info(f"Sync complete: {stats}")
        
        return stats

    async def _save_filing(self, db: AsyncSession, parsed: ParsedForm4) -> bool:
        """Guarda empresa, insiders y transacciones. Usa savepoint por filing."""
        if not parsed.insiders or not parsed.transactions:
            return False

        try:
            company = await self._get_or_create_company(db, parsed)
            primary_insider = await self._get_or_create_insider(db, parsed.insiders[0])
            primary_role = parsed.insiders[0].role

            saved_any = False
            for idx, pt in enumerate(parsed.transactions):
                if pt.transaction_code not in ("P", "S", "A", "M"):
                    continue

                # Accession único por transacción: accession + índice
                unique_accession = f"{parsed.accession_number}_{idx}"

                tx = Transaction(
                    company_id=company.id,
                    insider_id=primary_insider.id,
                    filing_url=parsed.filing_url,
                    accession_number=unique_accession,
                    transaction_date=pt.transaction_date or parsed.filed_date or datetime.utcnow(),
                    filed_date=parsed.filed_date or datetime.utcnow(),
                    transaction_type=self._map_type(pt.transaction_type),
                    insider_role=self._map_role(primary_role),
                    shares=pt.shares,
                    price_per_share=pt.price_per_share,
                    total_value=pt.total_value,
                    shares_owned_after=pt.shares_owned_after,
                    is_10b51_plan=pt.is_10b51,
                    is_derivative=pt.is_derivative,
                )
                db.add(tx)
                saved_any = True

            if saved_any:
                await db.flush()

            return saved_any

        except Exception as e:
            await db.rollback()
            logger.error(f"Error saving filing {parsed.accession_number}: {e}")
            return False

    async def _refresh_signals(self, db: AsyncSession) -> int:
        """Regenera señales para empresas con actividad reciente."""
        recent_cutoff = datetime.utcnow() - timedelta(days=7)
        result = await db.execute(
            select(Company)
            .join(Transaction)
            .where(Transaction.transaction_date >= recent_cutoff)
            .distinct()
        )
        companies = result.scalars().all()

        total = 0
        for company in companies:
            try:
                signals = await self.signal_engine.process_company_signals(db, company)
                await db.flush()
                total += len(signals)
            except Exception as e:
                await db.rollback()
                logger.error(f"Error generating signal for {company.name}: {e}")

        return total

    # ─── Helpers BD ────────────────────────────────────────────────────────────

    async def _filing_exists(self, db: AsyncSession, accession: str) -> bool:
        """Comprueba si ya tenemos transacciones de este accession number."""
        result = await db.execute(
            select(Transaction.id)
            .where(Transaction.accession_number.like(f"{accession}_%"))
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def _get_or_create_company(self, db: AsyncSession, parsed: ParsedForm4) -> Company:
        result = await db.execute(
            select(Company).where(Company.cik == parsed.company_cik)
        )
        company = result.scalar_one_or_none()
        if not company:
            company = Company(
                cik=parsed.company_cik,
                name=parsed.company_name,
                ticker=parsed.company_ticker,
            )
            db.add(company)
            await db.flush()
        elif parsed.company_ticker and not company.ticker:
            company.ticker = parsed.company_ticker
        return company

    async def _get_or_create_insider(self, db: AsyncSession, pi: ParsedInsider) -> Insider:
        result = await db.execute(
            select(Insider).where(Insider.cik == pi.cik)
        )
        insider = result.scalar_one_or_none()
        if not insider:
            insider = Insider(cik=pi.cik, name=pi.name)
            db.add(insider)
            await db.flush()
        return insider

    def _map_type(self, tx_type: str) -> TransactionType:
        return {
            "buy": TransactionType.BUY,
            "sell": TransactionType.SELL,
            "award": TransactionType.AWARD,
            "exercise": TransactionType.EXERCISE,
        }.get(tx_type, TransactionType.OTHER)

    def _map_role(self, role: str) -> InsiderRole:
        return {
            "CEO": InsiderRole.CEO,
            "CFO": InsiderRole.CFO,
            "COO": InsiderRole.COO,
            "President": InsiderRole.OFFICER,
            "Director": InsiderRole.DIRECTOR,
            "Major Shareholder >10%": InsiderRole.MAJOR_SHAREHOLDER,
            "VP": InsiderRole.VP,
            "Officer": InsiderRole.OFFICER,
        }.get(role, InsiderRole.OTHER)