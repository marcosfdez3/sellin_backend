from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.models.models import Signal, Transaction, TransactionType, SignalStrength
from app.schemas.schemas import SignalListOut, SignalOut, TransactionOut

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("/", response_model=SignalListOut)
async def get_signals(
    signal_type: TransactionType | None = Query(None, description="buy | sell"),
    strength: SignalStrength | None = Query(None, description="strong | medium | weak"),
    cluster_only: bool = Query(False, description="Solo señales con múltiples insiders"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """
    Feed principal de señales para la app.
    Ordenadas por score descendente (más relevantes primero).
    """
    query = (
        select(Signal)
        .options(selectinload(Signal.company))
        .order_by(desc(Signal.score), desc(Signal.created_at))
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
