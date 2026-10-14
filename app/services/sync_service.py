"""
sync_service.py
───────────────
Orquesta el pipeline completo de ingesta de Form 4s.

- sync_recent_filings: lee el feed de EDGAR (lo más reciente) cada pocos minutos.
- sync_day: procesa un día completo con el índice diario (rellena huecos).
Cada filing procesado se apunta en ProcessedFiling para no descargarlo dos veces.
"""
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import (
    Company, Insider, ProcessedFiling, Signal, Transaction, TransactionType, InsiderRole,
)
from app.services.edgar_fetcher import EdgarFetcher
from app.services.form4_parser import Form4Parser, ParsedForm4, ParsedInsider
from app.services.signal_engine import SignalEngine

logger = logging.getLogger(__name__)


class SyncService:

    def __init__(self):
        self.parser = Form4Parser()
        self.signal_engine = SignalEngine()

    @staticmethod
    def _new_stats() -> dict:
        return {
            "filings_fetched": 0,
            "filings_saved": 0,
            "filings_skipped": 0,
            "signals_generated": 0,
            "errors": 0,
        }

    async def sync_recent_filings(self, db: AsyncSession) -> dict:
        """
        Lee el feed de EDGAR página a página (lo más reciente primero) hasta
        llegar a filings ya procesados, y guarda los nuevos.
        """
        stats = self._new_stats()

        async with EdgarFetcher() as fetcher:
            pending = []
            for page in range(settings.SYNC_MAX_FEED_PAGES):
                filings = await fetcher.get_recent_form4_filings(start=page * 100)
                if not filings:
                    break
                stats["filings_fetched"] += len(filings)

                known = await self._processed_accessions(db, [f["accession_number"] for f in filings])
                pending.extend(f for f in filings if f["accession_number"] not in known)
                stats["filings_skipped"] += len(known)
                if known:
                    break  # a partir de aquí ya lo teníamos
            else:
                logger.warning(
                    f"Reached SYNC_MAX_FEED_PAGES ({settings.SYNC_MAX_FEED_PAGES}) "
                    "without finding known filings; the backfill will fill any gap"
                )

            await self._process_filings(db, fetcher, pending, stats)

        stats["signals_generated"] = await self._refresh_signals(db)
        logger.info(f"Sync complete: {stats}")
        return stats

    async def sync_day(self, db: AsyncSession, day: date) -> dict | None:
        """
        Procesa todos los Form 4 de un día usando el índice diario de EDGAR.
        Devuelve None si ese día no hay índice (fin de semana, festivo o aún no publicado).
        """
        stats = self._new_stats()

        async with EdgarFetcher() as fetcher:
            filings = await fetcher.get_daily_form4_filings(day)
            if filings is None:
                return None
            stats["filings_fetched"] = len(filings)

            known = await self._processed_accessions(db, [f["accession_number"] for f in filings])
            pending = [f for f in filings if f["accession_number"] not in known]
            stats["filings_skipped"] = len(known)
            logger.info(f"Backfill {day}: {len(pending)} new filings to process")

            await self._process_filings(db, fetcher, pending, stats)

        if stats["filings_saved"]:
            stats["signals_generated"] = await self._refresh_signals(db)
        logger.info(f"Backfill {day} complete: {stats}")
        return stats

    async def _process_filings(self, db: AsyncSession, fetcher: EdgarFetcher, filings: list[dict], stats: dict):
        """Descarga, parsea y guarda cada filing, con un commit por filing."""
        for meta in filings:
            accession = meta["accession_number"]
            try:
                result = await fetcher.get_form4_xml(meta["index_url"])
                saved = False
                if result:
                    xml_content, _ = result
                    parsed = self.parser.parse(
                        xml_content=xml_content,
                        accession_number=accession,
                        filed_date=meta.get("filed_date", ""),
                        filing_url=meta["index_url"],
                    )
                    if parsed:
                        saved = await self._save_filing(db, parsed)

                stats["filings_saved" if saved else "filings_skipped"] += 1
                # Procesado (guardado o descartado): no volver a descargarlo
                db.add(ProcessedFiling(accession_number=accession))
                await db.commit()

            except Exception as e:
                # Errores de red o de la SEC: no se marca como procesado y se reintenta en el próximo sync
                await db.rollback()
                logger.error(f"Error processing {meta.get('index_url')}: {e}")
                stats["errors"] += 1

    async def _save_filing(self, db: AsyncSession, parsed: ParsedForm4) -> bool:
        """Guarda empresa, insiders y transacciones del filing."""
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

                filed_date = parsed.filed_date or datetime.utcnow()
                tx_date = pt.transaction_date or filed_date
                if tx_date > filed_date + timedelta(days=1):
                    tx_date = filed_date  # error en el XML: no puede ser posterior a la presentación

                tx = Transaction(
                    company_id=company.id,
                    insider_id=primary_insider.id,
                    filing_url=parsed.filing_url,
                    accession_number=unique_accession,
                    transaction_date=tx_date,
                    filed_date=filed_date,
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
        """
        Recalcula las señales de las empresas con actividad en la ventana
        (CLUSTER_DAYS_WINDOW). Las señales de esa ventana se sustituyen,
        no se acumulan.
        """
        recent_cutoff = datetime.utcnow() - timedelta(days=settings.CLUSTER_DAYS_WINDOW)
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
                await db.execute(
                    delete(Signal).where(
                        Signal.company_id == company.id,
                        Signal.last_transaction_date >= recent_cutoff,
                    )
                )
                signals = await self.signal_engine.process_company_signals(db, company)
                await db.commit()
                total += len(signals)
            except Exception as e:
                await db.rollback()
                logger.error(f"Error generating signal for {company.name}: {e}")

        return total

    async def remove_duplicate_signals(self, db: AsyncSession) -> int:
        """
        Limpieza de datos antiguos: versiones anteriores creaban la misma señal
        en cada sync. Deja solo la más reciente de cada grupo idéntico.
        """
        keep = (
            select(func.max(Signal.id))
            .group_by(
                Signal.company_id,
                Signal.signal_type,
                Signal.first_transaction_date,
                Signal.last_transaction_date,
            )
        )
        result = await db.execute(delete(Signal).where(Signal.id.not_in(keep)))
        await db.commit()
        return result.rowcount or 0

    # ─── Helpers BD ────────────────────────────────────────────────────────────

    async def _processed_accessions(self, db: AsyncSession, accessions: list[str]) -> set[str]:
        """Devuelve cuáles de estos accession numbers ya se han procesado."""
        if not accessions:
            return set()
        result = await db.execute(
            select(ProcessedFiling.accession_number)
            .where(ProcessedFiling.accession_number.in_(accessions))
        )
        known = set(result.scalars().all())

        # Filings guardados antes de existir ProcessedFiling: solo tienen transacciones
        # (accession de la transacción = "<accession>_<índice>")
        for accession in set(accessions) - known:
            exists = await db.scalar(
                select(Transaction.id)
                .where(Transaction.accession_number.startswith(f"{accession}_", autoescape=True))
                .limit(1)
            )
            if exists is not None:
                known.add(accession)
        return known

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
