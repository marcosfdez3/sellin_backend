"""
form4_parser.py
───────────────
Parsea el XML de un Form 4 de la SEC y extrae las transacciones relevantes.

Estructura del Form 4 XML:
  <ownershipDocument>
    <issuer>              → datos de la empresa
    <reportingOwner>      → datos del insider (puede haber varios)
    <nonDerivativeTable>  → compras/ventas de acciones directas ← lo que más nos importa
    <derivativeTable>     → opciones y derivados
  </ownershipDocument>
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from lxml import etree

from app.services.edgar_fetcher import TRANSACTION_CODE_MAP

logger = logging.getLogger(__name__)


@dataclass
class ParsedInsider:
    cik: str
    name: str
    role: str
    is_director: bool = False
    is_officer: bool = False
    is_ten_percent: bool = False  # Accionista >10%


@dataclass
class ParsedTransaction:
    transaction_date: Optional[datetime]
    transaction_code: str          # P, S, A, M, etc.
    transaction_type: str          # buy, sell, award, exercise, other
    shares: float
    price_per_share: Optional[float]
    total_value: Optional[float]
    shares_owned_after: Optional[float]
    is_derivative: bool = False
    is_10b51: bool = False         # Plan de venta automático → menos relevante


@dataclass
class ParsedForm4:
    """Resultado de parsear un Form 4 completo."""
    accession_number: str
    filed_date: Optional[datetime]
    filing_url: str

    # Empresa emisora
    company_cik: str
    company_name: str
    company_ticker: Optional[str]

    # Insiders reportando
    insiders: list[ParsedInsider] = field(default_factory=list)

    # Transacciones
    transactions: list[ParsedTransaction] = field(default_factory=list)

    @property
    def has_open_market_buys(self) -> bool:
        return any(t.transaction_code == "P" for t in self.transactions)

    @property
    def has_open_market_sells(self) -> bool:
        return any(t.transaction_code == "S" for t in self.transactions)

    @property
    def total_buy_value(self) -> float:
        return sum(
            t.total_value or 0
            for t in self.transactions
            if t.transaction_code == "P"
        )

    @property
    def total_sell_value(self) -> float:
        return sum(
            t.total_value or 0
            for t in self.transactions
            if t.transaction_code == "S"
        )


class Form4Parser:
    """Parsea XML de Form 4 en objetos Python tipados."""

    def parse(
        self,
        xml_content: str,
        accession_number: str,
        filed_date: str,
        filing_url: str,
    ) -> Optional[ParsedForm4]:
        try:
            root = etree.fromstring(xml_content.encode())
            return self._extract(root, accession_number, filed_date, filing_url, xml_content)
        except etree.XMLSyntaxError as e:
            logger.error(f"XML parse error for {accession_number}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error parsing {accession_number}: {e}")
            return None

    def _extract(
        self,
        root: etree._Element,
        accession_number: str,
        filed_date: str,
        filing_url: str,
        raw_xml: str,
    ) -> ParsedForm4:

        # ── Empresa ───────────────────────────────────────────────────────────
        issuer = root.find("issuer")
        company_cik = self._text(issuer, "issuerCik", "").zfill(10)
        company_name = self._text(issuer, "issuerName", "Unknown")
        company_ticker = self._text(issuer, "issuerTradingSymbol")

        # ── Insiders ──────────────────────────────────────────────────────────
        insiders = []
        for owner in root.findall("reportingOwner"):
            insider = self._parse_insider(owner)
            if insider:
                insiders.append(insider)

        # ── Transacciones directas (acciones) ─────────────────────────────────
        transactions = []

        non_deriv_table = root.find("nonDerivativeTable")
        if non_deriv_table is not None:
            for tx_elem in non_deriv_table.findall("nonDerivativeTransaction"):
                tx = self._parse_transaction(tx_elem, is_derivative=False)
                if tx:
                    transactions.append(tx)

        # ── Transacciones derivadas (opciones) ────────────────────────────────
        deriv_table = root.find("derivativeTable")
        if deriv_table is not None:
            for tx_elem in deriv_table.findall("derivativeTransaction"):
                tx = self._parse_transaction(tx_elem, is_derivative=True)
                if tx:
                    transactions.append(tx)

        return ParsedForm4(
            accession_number=accession_number,
            filed_date=self._parse_date(filed_date),
            filing_url=filing_url,
            company_cik=company_cik,
            company_name=company_name,
            company_ticker=company_ticker,
            insiders=insiders,
            transactions=transactions,
        )

    def _parse_insider(self, owner_elem: etree._Element) -> Optional[ParsedInsider]:
        owner_id = owner_elem.find("reportingOwnerId")
        if owner_id is None:
            return None

        cik = self._text(owner_id, "rptOwnerCik", "").zfill(10)
        name = self._text(owner_id, "rptOwnerName", "Unknown")

        rel = owner_elem.find("reportingOwnerRelationship")
        is_director = self._bool(rel, "isDirector")
        is_officer = self._bool(rel, "isOfficer")
        is_ten_percent = self._bool(rel, "isTenPercentOwner")
        officer_title = self._text(rel, "officerTitle", "")

        return ParsedInsider(
            cik=cik,
            name=name,
            role=self._normalize_role(officer_title, is_director, is_officer, is_ten_percent),
            is_director=is_director,
            is_officer=is_officer,
            is_ten_percent=is_ten_percent,
        )

    def _parse_transaction(
        self,
        tx_elem: etree._Element,
        is_derivative: bool,
    ) -> Optional[ParsedTransaction]:

        # Fecha de la transacción
        tx_date_str = self._text(tx_elem, "transactionDate/value") or \
                      self._text(tx_elem, "transactionDate")
        tx_date = self._parse_date(tx_date_str)

        # Código de transacción (P=compra, S=venta, A=award, M=ejercicio, etc.)
        tx_coding = tx_elem.find("transactionCoding")
        tx_code = self._text(tx_coding, "transactionCode", "J")
        tx_type = TRANSACTION_CODE_MAP.get(tx_code, "other")

        # Amounts
        amounts = tx_elem.find("transactionAmounts")
        shares_elem = amounts.find("transactionShares/value") if amounts is not None else None
        price_elem = amounts.find("transactionPricePerShare/value") if amounts is not None else None
        direction_elem = amounts.find("transactionAcquiredDisposedCode/value") if amounts is not None else None

        try:
            shares = float(shares_elem.text) if shares_elem is not None and shares_elem.text else 0.0
        except (ValueError, AttributeError):
            shares = 0.0

        try:
            price = float(price_elem.text) if price_elem is not None and price_elem.text else None
        except (ValueError, AttributeError):
            price = None

        # Para ventas (D = Disposed), hacer shares negativo internamente
        direction = direction_elem.text.upper() if direction_elem is not None and direction_elem.text else "A"
        if direction == "D" and tx_type in ("sell",):
            pass  # ya está marcado como sell por el código

        total_value = shares * price if price and shares else None

        # Acciones poseídas después de la transacción
        post_elem = tx_elem.find("postTransactionAmounts/sharesOwnedFollowingTransaction/value")
        try:
            shares_after = float(post_elem.text) if post_elem is not None and post_elem.text else None
        except (ValueError, AttributeError):
            shares_after = None

        # Detectar plan 10b5-1 (ventas automáticas programadas → menos relevantes)
        footnotes_text = " ".join(
            (fn.text or "") for fn in tx_elem.findall(".//footnote")
        ).lower()
        is_10b51 = "10b5-1" in footnotes_text or "rule 10b5" in footnotes_text

        return ParsedTransaction(
            transaction_date=tx_date,
            transaction_code=tx_code,
            transaction_type=tx_type,
            shares=shares,
            price_per_share=price,
            total_value=total_value,
            shares_owned_after=shares_after,
            is_derivative=is_derivative,
            is_10b51=is_10b51,
        )

    # ─── Helpers ───────────────────────────────────────────────────────────────

    def _text(
        self,
        elem: Optional[etree._Element],
        path: str,
        default: Optional[str] = None,
    ) -> Optional[str]:
        if elem is None:
            return default
        found = elem.find(path)
        if found is None or found.text is None:
            return default
        return found.text.strip()

    def _bool(self, elem: Optional[etree._Element], tag: str) -> bool:
        val = self._text(elem, tag, "0")
        return val in ("1", "true", "True", "yes")

    def _parse_date(self, date_str: Optional[str]) -> Optional[datetime]:
        if not date_str:
            return None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
            try:
                return datetime.strptime(date_str.strip(), fmt)
            except ValueError:
                continue
        logger.warning(f"Could not parse date: {date_str}")
        return None

    def _normalize_role(
        self,
        title: str,
        is_director: bool,
        is_officer: bool,
        is_ten_percent: bool,
    ) -> str:
        title_upper = title.upper()
        if "CEO" in title_upper or "CHIEF EXECUTIVE" in title_upper:
            return "CEO"
        if "CFO" in title_upper or "CHIEF FINANCIAL" in title_upper:
            return "CFO"
        if "COO" in title_upper or "CHIEF OPERATING" in title_upper:
            return "COO"
        if "PRESIDENT" in title_upper:
            return "President"
        if "VP" in title_upper or "VICE PRESIDENT" in title_upper:
            return "VP"
        if is_ten_percent:
            return "Major Shareholder >10%"
        if is_director:
            return "Director"
        if is_officer:
            return title or "Officer"
        return title or "Other"
