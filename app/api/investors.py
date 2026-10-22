from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import InvestorMove
from app.schemas.schemas import InvestorMoveOut

investors_router = APIRouter(prefix="/investors", tags=["investors"])


@investors_router.get("/moves", response_model=list[InvestorMoveOut])
async def get_investor_moves(
    limit: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Principales movimientos de los grandes inversores en su último 13F, los más grandes primero."""
    moves = await db.scalars(
        select(InvestorMove)
        .order_by(desc(func.abs(InvestorMove.value_change)))
        .limit(limit)
    )
    return moves.all()
