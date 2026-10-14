from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_db
from app.models.models import Signal, Transaction, TransactionType, SignalStrength
from app.schemas.schemas import SignalListOut, SignalOut, TransactionOut

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("/", response_model=SignalListOut)
async def get_signals(
    signal_type: TransactionType | None = Query(None, description="buy | sell"),
    strength: SignalStrength | None = Query(None, description="strong | medium | weak"),
    cluster_only: bool = Query(False, description="Solo señales con múltiples insiders"),
    sort: Literal["recent", "score"] = Query("recent", description="recent: más recientes primero | score: mejor puntuación"),
    days: int = Query(settings.FEED_DEFAULT_DAYS, ge=1, le=365, description="Solo señales con actividad en los últimos N días"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """
    Feed principal de señales para la app.
    Por defecto, las más recientes primero (fecha de la última transacción).
    """
    if sort == "recent":
        order = (desc(Signal.last_transaction_date), desc(Signal.score))
    else:
        order = (desc(Signal.score), desc(Signal.last_transaction_date))

    query = (
        select(Signal)
        .options(selectinload(Signal.company))
        .where(Signal.last_transaction_date >= datetime.utcnow() - timedelta(days=days))
        .order_by(*order)
    )

    if signal_type:
        query = query.where(Signal.signal_type == signal_type)
    if strength:
        query = query.where(Signal.strength == strength)
    if cluster_only:
        query = query.where(Signal.is_cluster == True)

    # Total para paginación
    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()

    # Paginación
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    signals = result.scalars().all()

    return SignalListOut(
        signals=[SignalOut.model_validate(s) for s in signals],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{signal_id}", response_model=SignalOut)
async def get_signal_detail(signal_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Signal)
        .options(selectinload(Signal.company))
        .where(Signal.id == signal_id)
    )
    signal = result.scalar_one_or_none()
    if not signal:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Signal not found")
    return SignalOut.model_validate(signal)
