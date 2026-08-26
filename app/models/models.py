from datetime import datetime
from sqlalchemy import (
    String, Integer, Float, Boolean, DateTime,
    ForeignKey, Enum, Text, Index
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
import enum
from app.core.database import Base


class TransactionType(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"
    AWARD = "award"       # Stock awards (no contar como señal)
    EXERCISE = "exercise" # Ejercicio de opciones (cuidado con el sesgo)
    OTHER = "other"


class InsiderRole(str, enum.Enum):
    CEO = "CEO"
    CFO = "CFO"
    COO = "COO"
    DIRECTOR = "director"
    VP = "VP"
    OFFICER = "officer"
    MAJOR_SHAREHOLDER = "major_shareholder"
    OTHER = "other"


class SignalStrength(str, enum.Enum):
    STRONG = "strong"   # Cluster o compra muy grande
    MEDIUM = "medium"   # Compra significativa única
    WEAK = "weak"       # Compra pequeña o insider menor


# ─── Company ──────────────────────────────────────────────────────────────────

class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cik: Mapped[str] = mapped_column(String(10), unique=True, index=True)  # ID interno de la SEC
    ticker: Mapped[str | None] = mapped_column(String(10), index=True)
    name: Mapped[str] = mapped_column(String(255))
    sic_code: Mapped[str | None] = mapped_column(String(10))  # Sector industrial
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="company")
    signals: Mapped[list["Signal"]] = relationship(back_populates="company")


# ─── Insider ───────────────────────────────────────────────────────────────────

class Insider(Base):
    __tablename__ = "insiders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cik: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="insider")


# ─── Transaction (Form 4) ──────────────────────────────────────────────────────

class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Claves foráneas
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    insider_id: Mapped[int] = mapped_column(ForeignKey("insiders.id"), index=True)

    # Datos del Form 4
    filing_url: Mapped[str] = mapped_column(String(500))
    accession_number: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    transaction_date: Mapped[datetime] = mapped_column(DateTime, index=True)
    filed_date: Mapped[datetime] = mapped_column(DateTime)

    # Detalles de la transacción
    transaction_type: Mapped[TransactionType] = mapped_column(Enum(TransactionType))
    insider_role: Mapped[InsiderRole] = mapped_column(Enum(InsiderRole), default=InsiderRole.OTHER)
    shares: Mapped[float] = mapped_column(Float)
    price_per_share: Mapped[float | None] = mapped_column(Float)
    total_value: Mapped[float | None] = mapped_column(Float)  # shares * price
    shares_owned_after: Mapped[float | None] = mapped_column(Float)

    # Flags de calidad de señal
    is_10b51_plan: Mapped[bool] = mapped_column(Boolean, default=False)  # Venta automática, menos relevante
    is_derivative: Mapped[bool] = mapped_column(Boolean, default=False)  # Opciones

    # Raw XML para debug/re-parseo
    raw_xml: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    company: Mapped["Company"] = relationship(back_populates="transactions")
    insider: Mapped["Insider"] = relationship(back_populates="transactions")

    __table_args__ = (
        Index("ix_transactions_company_date", "company_id", "transaction_date"),
        Index("ix_transactions_type_date", "transaction_type", "transaction_date"),
    )


# ─── Signal ────────────────────────────────────────────────────────────────────

class Signal(Base):
    """
    Una señal es una interpretación procesada de una o varias transacciones.
    Es lo que ve el usuario final en la app.
    """
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)

    signal_type: Mapped[TransactionType] = mapped_column(Enum(TransactionType))  # buy o sell
    strength: Mapped[SignalStrength] = mapped_column(Enum(SignalStrength))
    score: Mapped[float] = mapped_column(Float)  # 0-100, para ordenar

    # Resumen de la señal
    total_value: Mapped[float] = mapped_column(Float)
    num_insiders: Mapped[int] = mapped_column(Integer, default=1)
    is_cluster: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[str] = mapped_column(String(500))  # Texto legible para la app

    # Ventana temporal de las transacciones que generaron la señal
    first_transaction_date: Mapped[datetime] = mapped_column(DateTime)
    last_transaction_date: Mapped[datetime] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    company: Mapped["Company"] = relationship(back_populates="signals")

    __table_args__ = (
        Index("ix_signals_type_score", "signal_type", "score"),
        Index("ix_signals_created", "created_at"),
    )
