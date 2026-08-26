from fastapi import APIRouter, Depends, Query, Path
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.models.models import Company, Transaction, TransactionType
from app.schemas.schemas import TransactionOut, CompanyOut

router = APIRouter(tags=["transactions"])

# ─── Transactions ──────────────────────────────────────────────────────────────

transactions_router = APIRouter(prefix="/transactions")


@transactions_router.get("/", response_model=list[TransactionOut])
async def get_transactions(
    transaction_type: TransactionType | None = None,
    ticker: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Transacciones recientes sin procesar. Útil para el detalle de empresa."""
    query = (
        select(Transaction)
        .options(
            selectinload(Transaction.company),
            selectinload(Transaction.insider),
        )
        .order_by(desc(Transaction.transaction_date))
        .limit(limit)
    )

    if transaction_type:
        query = query.where(Transaction.transaction_type == transaction_type)

    if ticker:
        query = query.join(Company).where(
            Company.ticker.ilike(ticker)
        )

    result = await db.execute(query)
    return [TransactionOut.model_validate(t) for t in result.scalars().all()]


# ─── Companies ─────────────────────────────────────────────────────────────────

companies_router = APIRouter(prefix="/companies")


@companies_router.get("/search", response_model=list[CompanyOut])
async def search_companies(
    q: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Company)
        .where(
            (Company.ticker.ilike(f"%{q}%")) |
            (Company.name.ilike(f"%{q}%"))
        )
        .limit(20)
    )
    return [CompanyOut.model_validate(c) for c in result.scalars().all()]


@companies_router.get("/{ticker}/transactions", response_model=list[TransactionOut])
async def get_company_transactions(
    ticker: str = Path(...),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Transaction)
        .options(
            selectinload(Transaction.company),
            selectinload(Transaction.insider),
        )
        .join(Company)
        .where(Company.ticker.ilike(ticker))
        .order_by(desc(Transaction.transaction_date))
        .limit(limit)
    )
    return [TransactionOut.model_validate(t) for t in result.scalars().all()]
