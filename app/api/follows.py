from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.models import Company, Follow, Signal, User
from app.schemas.schemas import FollowOut, SignalOut

follows_router = APIRouter(prefix="/follows", tags=["follows"])


@follows_router.get("/", response_model=list[FollowOut])
async def get_follows(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Empresas que sigue el usuario (las últimas seguidas primero), con su señal más reciente."""
    follows = (await db.scalars(
        select(Follow)
        .options(selectinload(Follow.company))
        .where(Follow.user_id == user.id)
        .order_by(desc(Follow.created_at))
    )).all()
    if not follows:
        return []

    # Señal más reciente de cada empresa seguida
    signals = await db.scalars(
        select(Signal)
        .options(selectinload(Signal.company))
        .where(Signal.company_id.in_([f.company_id for f in follows]))
        .order_by(desc(Signal.last_transaction_date), desc(Signal.score))
    )
    latest: dict[int, Signal] = {}
    for signal in signals:
        latest.setdefault(signal.company_id, signal)

    return [
        FollowOut(
            company=f.company,
            followed_at=f.created_at,
            latest_signal=SignalOut.model_validate(latest[f.company_id]) if f.company_id in latest else None,
        )
        for f in follows
    ]


@follows_router.put("/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
async def follow_company(
    company_id: int = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Sigue una empresa. Si ya la seguía, no hace nada."""
    if await db.get(Company, company_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Empresa no encontrada")

    existing = await db.scalar(
        select(Follow).where(Follow.user_id == user.id, Follow.company_id == company_id)
    )
    if existing is None:
        db.add(Follow(user_id=user.id, company_id=company_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@follows_router.delete("/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unfollow_company(
    company_id: int = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Deja de seguir una empresa. Si no la seguía, no hace nada."""
    follow = await db.scalar(
        select(Follow).where(Follow.user_id == user.id, Follow.company_id == company_id)
    )
    if follow is not None:
        await db.delete(follow)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
