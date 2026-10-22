from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Path
from sqlalchemy import case, desc, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.models.models import Company, Signal, Transaction, TransactionType
from app.schemas.schemas import (
    CompanyActivityOut, CompanyOut, CompanyOverviewOut, CompanySearchOut,
    SignalOut, TransactionOut,
)
from app.services import sec_companies

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


@companies_router.get("/search", response_model=list[CompanySearchOut])
async def search_companies(
    q: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
):
    """
    Busca entre todas las empresas cotizadas de la SEC (por ticker o nombre),
    no solo las que tienen actividad. id es None si aún no están en la BD.
    """
    listed = await sec_companies.search(q)
    if not listed and not await sec_companies.listed_companies():
        # La SEC no responde: buscar al menos en nuestra BD
        result = await db.execute(
            select(Company)
            .where(Company.ticker.ilike(f"%{q}%") | Company.name.ilike(f"%{q}%"))
            .limit(20)
        )
        return [CompanySearchOut(id=c.id, cik=c.cik, ticker=c.ticker, name=c.name)
                for c in result.scalars().all()]

    ids = dict((await db.execute(
        select(Company.cik, Company.id).where(Company.cik.in_([c.cik for c in listed]))
    )).all())
    return [CompanySearchOut(id=ids.get(c.cik), cik=c.cik, ticker=c.ticker, name=c.name)
            for c in listed]


@companies_router.get("/popular", response_model=list[CompanyActivityOut])
async def popular_companies(
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """Empresas con más compras y ventas de insiders en los últimos días."""
    since = datetime.utcnow() - timedelta(days=days)
    value = func.coalesce(Transaction.total_value, 0)
    rows = (await db.execute(
        select(
            Transaction.company_id,
            func.count().label("num_transactions"),
            func.count(distinct(Transaction.insider_id)).label("num_insiders"),
            func.sum(case((Transaction.transaction_type == TransactionType.BUY, value), else_=0)).label("buy_value"),
            func.sum(case((Transaction.transaction_type == TransactionType.SELL, value), else_=0)).label("sell_value"),
        )
        .where(
            Transaction.transaction_date >= since,
            Transaction.transaction_type.in_([TransactionType.BUY, TransactionType.SELL]),
            Transaction.is_derivative == False,
        )
        .group_by(Transaction.company_id)
        .order_by(desc("num_transactions"), desc(func.sum(value)))
        .limit(limit)
    )).all()

    companies = {c.id: c for c in (await db.scalars(
        select(Company).where(Company.id.in_([r.company_id for r in rows]))
    )).all()}
    return [
        CompanyActivityOut(
            company=CompanyOut.model_validate(companies[r.company_id]),
            num_transactions=r.num_transactions,
            num_insiders=r.num_insiders,
            buy_value=r.buy_value or 0,
            sell_value=r.sell_value or 0,
        )
        for r in rows
    ]


@companies_router.get("/by-cik/{cik}", response_model=CompanyOverviewOut)
async def company_overview(
    cik: str = Path(..., pattern=r"^\d{1,10}$"),
    db: AsyncSession = Depends(get_db),
):
    """
    Ficha de una empresa. Si es una cotizada que aún no está en la BD
    (sin actividad de insiders), se crea para poder seguirla.
    """
    cik = cik.zfill(10)
    company = await db.scalar(select(Company).where(Company.cik == cik))
    if company is None:
        listed = await sec_companies.find_by_cik(cik)
        if listed is None:
            raise HTTPException(status_code=404, detail="Empresa no encontrada")
        company = Company(cik=cik, ticker=listed.ticker, name=listed.name)
        db.add(company)
        await db.flush()

    signals = (await db.scalars(
        select(Signal)
        .options(selectinload(Signal.company))
        .where(Signal.company_id == company.id)
        .order_by(desc(Signal.last_transaction_date))
        .limit(10)
    )).all()
    transactions = (await db.scalars(
        select(Transaction)
        .options(selectinload(Transaction.company), selectinload(Transaction.insider))
        .where(Transaction.company_id == company.id)
        .order_by(desc(Transaction.transaction_date))
        .limit(20)
    )).all()
    return CompanyOverviewOut(
        company=CompanyOut.model_validate(company),
        signals=[SignalOut.model_validate(s) for s in signals],
        transactions=[TransactionOut.model_validate(t) for t in transactions],
    )


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
