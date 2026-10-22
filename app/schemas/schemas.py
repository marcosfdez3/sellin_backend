from datetime import datetime
from pydantic import BaseModel, EmailStr, Field
from app.models.models import TransactionType, SignalStrength, InsiderRole


class CompanyOut(BaseModel):
    id: int
    cik: str
    ticker: str | None
    name: str

    model_config = {"from_attributes": True}


class InsiderOut(BaseModel):
    id: int
    cik: str
    name: str

    model_config = {"from_attributes": True}


class TransactionOut(BaseModel):
    id: int
    company: CompanyOut
    insider: InsiderOut
    transaction_date: datetime
    filed_date: datetime
    transaction_type: TransactionType
    insider_role: InsiderRole
    shares: float
    price_per_share: float | None
    total_value: float | None
    shares_owned_after: float | None
    is_10b51_plan: bool
    is_derivative: bool
    filing_url: str

    model_config = {"from_attributes": True}


class SignalOut(BaseModel):
    id: int
    company: CompanyOut
    signal_type: TransactionType
    strength: SignalStrength
    score: float
    total_value: float
    num_insiders: int
    is_cluster: bool
    summary: str
    first_transaction_date: datetime
    last_transaction_date: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class SignalListOut(BaseModel):
    signals: list[SignalOut]
    total: int
    page: int
    page_size: int


class FollowOut(BaseModel):
    company: CompanyOut
    followed_at: datetime
    latest_signal: SignalOut | None  # señal más reciente de la empresa, si tiene


class InvestorMoveOut(BaseModel):
    investor: str
    fund: str
    ticker: str | None
    issuer: str
    action: str             # new | add | reduce | exit
    value_change: float     # USD; negativo si reduce o vende
    pct_change: float | None
    period: datetime
    filed_at: datetime

    model_config = {"from_attributes": True}


class SyncStatsOut(BaseModel):
    filings_fetched: int
    filings_saved: int
    filings_skipped: int
    signals_generated: int
    errors: int


# ─── Auth ─────────────────────────────────────────────────────────────────────

class UserCredentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)  # bcrypt usa como máximo 72 bytes


class GoogleLoginIn(BaseModel):
    id_token: str


class UserOut(BaseModel):
    id: int
    email: str
    created_at: datetime

    model_config = {"from_attributes": True}


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
