"""
investor_moves.py
─────────────────
Principales movimientos de los grandes inversores, a partir de sus 13F.

El 13F es la cartera que los gestores de más de $100M declaran a la SEC
cada trimestre (hasta 45 días después del cierre). Comparando los dos
últimos de cada inversor se ve qué han comprado, aumentado, reducido o
vendido del todo.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from lxml import etree
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import CusipTicker, InvestorMove
from app.services.edgar_fetcher import EdgarFetcher

logger = logging.getLogger(__name__)

# (nombre que ve el usuario, CIK del fondo en la SEC)
# Fuera: Michael Burry (Scion) y David Einhorn (Greenlight) ya no presentan 13F.
FAMOUS_INVESTORS = [
    ("Warren Buffett", "0001067983"),         # Berkshire Hathaway
    ("Bill Ackman", "0001336528"),            # Pershing Square
    ("Ray Dalio", "0001350694"),              # Bridgewater Associates
    ("Cathie Wood", "0001697748"),            # ARK Investment Management
    ("Stanley Druckenmiller", "0001536411"),  # Duquesne Family Office
    ("David Tepper", "0001656456"),           # Appaloosa
    ("Carl Icahn", "0000921669"),
    ("George Soros", "0001029160"),           # Soros Fund Management
    ("Seth Klarman", "0001061768"),           # Baupost Group
    ("Dan Loeb", "0001040273"),               # Third Point
    ("Li Lu", "0001709323"),                  # Himalaya Capital
    ("Chase Coleman", "0001167483"),          # Tiger Global
]

# Si el último 13F es más antiguo, el inversor se omite (ha dejado de presentarlos).
# 230 días: el del trimestre anterior sigue valiendo hasta que sale el siguiente.
MAX_PERIOD_AGE_DAYS = 230
# Movimientos más grandes que se guardan por inversor
MOVES_PER_INVESTOR = 3
# Cambios menores en el nº de acciones no cuentan como aumento/reducción
MIN_SHARES_CHANGE = 0.10

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
OPENFIGI_BATCH = 10   # máximo de CUSIPs por petición sin API key
OPENFIGI_PAUSE = 2.5  # sin API key: 25 peticiones por minuto


@dataclass
class Move:
    investor: str
    fund: str
    cusip: str
    issuer: str
    action: str
    value_change: float
    pct_change: float | None
    period: datetime
    filed_at: datetime


class InvestorMovesService:

    async def fetch_moves(self) -> list[Move]:
        """Descarga los 13F de cada inversor y calcula sus principales movimientos."""
        moves: list[Move] = []
        async with EdgarFetcher() as fetcher:
            for investor, cik in FAMOUS_INVESTORS:
                try:
                    moves += await self._investor_moves(fetcher, investor, cik)
                except Exception as e:
                    logger.error(f"13F de {investor} failed: {e}")
        return moves

    async def save(self, db: AsyncSession, moves: list[Move]) -> int:
        """Sustituye los movimientos guardados por [moves], con su ticker."""
        if not moves:
            return 0  # EDGAR caído o sin datos: mejor dejar los de ayer
        tickers = await self._resolve_tickers(db, {m.cusip for m in moves})

        await db.execute(delete(InvestorMove))
        db.add_all(
            InvestorMove(
                investor=m.investor,
                fund=m.fund,
                cusip=m.cusip,
                ticker=tickers.get(m.cusip),
                issuer=m.issuer,
                action=m.action,
                value_change=m.value_change,
                pct_change=m.pct_change,
                period=m.period,
                filed_at=m.filed_at,
            )
            for m in moves
        )
        await db.commit()
        return len(moves)

    # ─── 13F ───────────────────────────────────────────────────────────────────

    async def _investor_moves(self, fetcher: EdgarFetcher, investor: str, cik: str) -> list[Move]:
        fund, filings = await fetcher.get_13f_filings(cik)
        if len(filings) < 2:
            return []
        latest, previous = filings[0], filings[1]

        period = datetime.strptime(latest["report_date"], "%Y-%m-%d")
        if datetime.utcnow() - period > timedelta(days=MAX_PERIOD_AGE_DAYS):
            logger.info(f"13F de {investor} demasiado antiguo ({latest['report_date']}), se omite")
            return []

        current = self.parse_information_table(
            await fetcher.get_13f_information_table(cik, latest["accession_number"])
        )
        before = self.parse_information_table(
            await fetcher.get_13f_information_table(cik, previous["accession_number"])
        )

        moves = [
            Move(
                investor=investor,
                fund=fund or investor,
                cusip=cusip,
                issuer=issuer,
                action=action,
                value_change=value_change,
                pct_change=pct_change,
                period=period,
                filed_at=datetime.strptime(latest["filed_date"], "%Y-%m-%d"),
            )
            for cusip, issuer, action, value_change, pct_change in self.compare(before, current)
        ]
        moves.sort(key=lambda m: abs(m.value_change), reverse=True)
        return moves[:MOVES_PER_INVESTOR]

    @staticmethod
    def parse_information_table(xml: str) -> dict[str, dict]:
        """
        Posiciones en acciones de un 13F agrupadas por CUSIP:
        {cusip: {"issuer", "shares", "value"}}. Ignora opciones y bonos.
        """
        root = etree.fromstring(xml.encode())
        holdings: dict[str, dict] = {}
        for row in root.iter("{*}infoTable"):
            if row.find("{*}putCall") is not None:
                continue  # opciones put/call
            if (row.findtext(".//{*}sshPrnamtType") or "").strip() != "SH":
                continue  # bonos (PRN)
            cusip = (row.findtext("{*}cusip") or "").strip().upper()
            if not cusip:
                continue
            h = holdings.setdefault(cusip, {
                "issuer": (row.findtext("{*}nameOfIssuer") or cusip).strip(),
                "shares": 0.0,
                "value": 0.0,
            })
            h["shares"] += float(row.findtext(".//{*}sshPrnamt") or 0)
            h["value"] += float(row.findtext("{*}value") or 0)

        # Desde 2023 el valor va en dólares, pero algunos fondos siguen
        # declarándolo en miles: se nota en que el precio por acción implícito
        # (mediano) sale por debajo de $1
        prices = sorted(h["value"] / h["shares"] for h in holdings.values() if h["shares"] > 0)
        if prices and prices[len(prices) // 2] < 1:
            for h in holdings.values():
                h["value"] *= 1000
        return holdings

    @staticmethod
    def compare(before: dict[str, dict], current: dict[str, dict]):
        """
        Movimientos entre dos carteras:
        (cusip, empresa, acción, cambio en USD, % de cambio en acciones).
        """
        for cusip in before.keys() | current.keys():
            prev = before.get(cusip)
            cur = current.get(cusip)
            prev_shares = prev["shares"] if prev else 0
            cur_shares = cur["shares"] if cur else 0

            if prev_shares <= 0 and cur_shares > 0:
                yield cusip, cur["issuer"], "new", cur["value"], None
            elif prev_shares > 0 and cur_shares <= 0:
                yield cusip, prev["issuer"], "exit", -prev["value"], -100.0
            elif prev_shares > 0 and cur_shares > 0:
                ratio = cur_shares / prev_shares - 1
                if abs(ratio) < MIN_SHARES_CHANGE:
                    continue
                price = cur["value"] / cur_shares
                yield (
                    cusip,
                    cur["issuer"],
                    "add" if ratio > 0 else "reduce",
                    (cur_shares - prev_shares) * price,
                    round(ratio * 100, 1),
                )

    # ─── CUSIP → ticker (OpenFIGI) ─────────────────────────────────────────────

    async def _resolve_tickers(self, db: AsyncSession, cusips: set[str]) -> dict[str, str | None]:
        """Ticker de cada CUSIP; los que no se conocían se piden a OpenFIGI y se guardan."""
        known = {
            row.cusip: row.ticker
            for row in (await db.scalars(select(CusipTicker).where(CusipTicker.cusip.in_(cusips)))).all()
        }
        missing = sorted(cusips - known.keys())
        if not missing:
            return known

        async with httpx.AsyncClient(timeout=30.0) as client:
            for i in range(0, len(missing), OPENFIGI_BATCH):
                if i:
                    await asyncio.sleep(OPENFIGI_PAUSE)
                batch = missing[i:i + OPENFIGI_BATCH]
                try:
                    results = await self._openfigi(client, batch)
                except Exception as e:
                    logger.error(f"OpenFIGI failed: {e}")
                    break  # se reintenta en la próxima ejecución
                for cusip, result in zip(batch, results):
                    data = result.get("data") or []
                    best = next((d for d in data if d.get("securityType") == "Common Stock"), None)
                    best = best or (data[0] if data else {})
                    known[cusip] = best.get("ticker")
                    db.add(CusipTicker(cusip=cusip, ticker=best.get("ticker"), name=best.get("name")))
        await db.flush()
        return known

    async def _openfigi(self, client: httpx.AsyncClient, cusips: list[str]) -> list[dict]:
        body = [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in cusips]
        response = await client.post(OPENFIGI_URL, json=body)
        if response.status_code == 429:  # límite por minuto: espera y reintenta una vez
            await asyncio.sleep(60)
            response = await client.post(OPENFIGI_URL, json=body)
        response.raise_for_status()
        return response.json()
