"""
signal_engine.py
────────────────
Toma transacciones procesadas y genera señales de compra/venta con un score.
Este es el diferenciador clave de la app frente a mostrar datos crudos.

Criterios de scoring:
  - Tipo de insider (CEO/CFO > Director > Officer)
  - Tamaño de la transacción en USD
  - Cluster (varios insiders comprando en ventana corta)
  - Historial del insider (¿ha acertado antes?)
  - Ratio compra vs holdings totales
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import (
    Company, Insider, Transaction, Signal,
    TransactionType, SignalStrength
)

logger = logging.getLogger(__name__)

# Pesos por rol — los directivos con más información valen más
ROLE_WEIGHTS = {
    "CEO": 1.5,
    "CFO": 1.4,
    "COO": 1.3,
    "President": 1.3,
    "Director": 1.1,
    "Major Shareholder >10%": 1.2,
    "VP": 1.0,
    "Officer": 1.0,
    "Other": 0.8,
}


class SignalEngine:

    async def process_company_signals(
        self,
        db: AsyncSession,
        company: Company,
    ) -> list[Signal]:
        """
        Analiza todas las transacciones recientes de una empresa
        y genera/actualiza las señales correspondientes.
        """
        window_start = datetime.utcnow() - timedelta(days=settings.CLUSTER_DAYS_WINDOW)

        # Obtener transacciones recientes de compra y venta en mercado abierto
        result = await db.execute(
            select(Transaction)
            .where(
                Transaction.company_id == company.id,
                Transaction.transaction_date >= window_start,
Transaction.transaction_type.in_([TransactionType.BUY, TransactionType.SELL, TransactionType.EXERCISE]),                Transaction.is_10b51_plan == False,  # Excluir planes automáticos
                Transaction.is_derivative == False,   # Excluir opciones
            )
            .order_by(Transaction.transaction_date.asc())
        )
        transactions = result.scalars().all()

        if not transactions:
            return []

        signals = []

        # ── Señal de compra ───────────────────────────────────────────────────
        buys = [t for t in transactions if t.transaction_type == TransactionType.BUY]
        if buys:
            signal = await self._build_signal(db, company, buys, TransactionType.BUY)
            if signal:
                signals.append(signal)

        # ── Señal de venta ────────────────────────────────────────────────────
        sells = [t for t in transactions if t.transaction_type == TransactionType.SELL]
        if sells:
            signal = await self._build_signal(db, company, sells, TransactionType.SELL)
            if signal:
                signals.append(signal)

        return signals

    async def _build_signal(
        self,
        db: AsyncSession,
        company: Company,
        transactions: list[Transaction],
        signal_type: TransactionType,
    ) -> Signal | None:

        # Filtrar por valor mínimo para reducir ruido
        significant = [
            t for t in transactions
            if (t.total_value or 0) >= settings.MIN_TRANSACTION_VALUE_USD
        ]
        if not significant:
            return None

        total_value = sum(t.total_value or 0 for t in significant)
        num_insiders = len({t.insider_id for t in significant})
        is_cluster = num_insiders >= settings.CLUSTER_MIN_INSIDERS

        score = self._calculate_score(significant, total_value, is_cluster, db)
        strength = self._determine_strength(score, total_value, is_cluster)

        summary = self._build_summary(
            signal_type, significant, total_value, num_insiders, is_cluster, company
        )

        signal = Signal(
            company_id=company.id,
            signal_type=signal_type,
            strength=strength,
            score=score,
            total_value=total_value,
            num_insiders=num_insiders,
            is_cluster=is_cluster,
            summary=summary,
            first_transaction_date=min(t.transaction_date for t in significant),
            last_transaction_date=max(t.transaction_date for t in significant),
        )

        db.add(signal)
        return signal

    def _calculate_score(
        self,
        transactions: list[Transaction],
        total_value: float,
        is_cluster: bool,
        db: AsyncSession,
    ) -> float:
        """
        Score de 0 a 100.
        Componentes:
          - 40% tamaño en USD
          - 30% importancia del rol del insider
          - 20% cluster bonus
          - 10% concentración (% de holdings)
        """
        score = 0.0

        # ── Componente 1: Tamaño (0-40 pts) ──────────────────────────────────
        # $50k → 5pts, $250k → 20pts, $1M → 35pts, $5M+ → 40pts
        value_score = min(40, (total_value / 5_000_000) * 40)
        score += value_score

        # ── Componente 2: Rol del insider (0-30 pts) ──────────────────────────
        max_role_weight = max(
            ROLE_WEIGHTS.get(t.insider_role.value if t.insider_role else "Other", 0.8)
            for t in transactions
        )
        score += max_role_weight * 20  # max 30 pts para CEO (1.5 * 20)

        # ── Componente 3: Cluster (0-20 pts) ──────────────────────────────────
        if is_cluster:
            num_insiders = len({t.insider_id for t in transactions})
            cluster_score = min(20, (num_insiders - 1) * 8)
            score += cluster_score

        # ── Componente 4: Concentración (0-10 pts) ────────────────────────────
        # Si el insider tiene shares_owned_after, calculamos qué % representa la compra
        for t in transactions:
            if t.shares_owned_after and t.shares_owned_after > 0 and t.shares:
                pct_of_holdings = (t.shares / t.shares_owned_after) * 100
                score += min(10, pct_of_holdings * 2)
                break

        return min(100, round(score, 1))

    def _determine_strength(
        self,
        score: float,
        total_value: float,
        is_cluster: bool,
    ) -> SignalStrength:
        if score >= 65 or is_cluster or total_value >= 500_000:
            return SignalStrength.STRONG
        if score >= 35 or total_value >= 100_000:
            return SignalStrength.MEDIUM
        return SignalStrength.WEAK

    def _build_summary(
        self,
        signal_type: TransactionType,
        transactions: list[Transaction],
        total_value: float,
        num_insiders: int,
        is_cluster: bool,
        company: Company,
    ) -> str:
        """Genera el texto legible que verá el usuario en la app."""
        ticker = company.ticker or company.name
        action = "compró" if signal_type == TransactionType.BUY else "vendió"
        value_str = self._format_value(total_value)

        if is_cluster:
            return (
                f"{num_insiders} insiders de {ticker} {action} "
                f"un total de {value_str} en los últimos {settings.CLUSTER_DAYS_WINDOW} días"
            )

        # Transacción individual
        t = transactions[0]
        role = t.insider_role.value if t.insider_role else "insider"
        return f"{role} de {ticker} {action} {value_str} en acciones"

    def _format_value(self, value: float) -> str:
        if value >= 1_000_000:
            return f"${value / 1_000_000:.1f}M"
        if value >= 1_000:
            return f"${value / 1_000:.0f}K"
        return f"${value:.0f}"
