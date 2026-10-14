"""
edgar_fetcher.py
────────────────
Responsable de comunicarse con la API de EDGAR/SEC y descargar Form 4s.
"""
import asyncio
import logging
import re
from datetime import date, datetime, timedelta

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

TRANSACTION_CODE_MAP = {
    "P": "buy",
    "S": "sell",
    "A": "award",
    "M": "exercise",
    "G": "other",
    "D": "other",
    "F": "other",
    "I": "other",
    "J": "other",
}


class EdgarFetcher:
    """Cliente HTTP para la API de EDGAR."""

    def __init__(self):
        self.base_headers = {
            "User-Agent": settings.EDGAR_USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
        }
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    async def _get(self, url: str) -> httpx.Response:
        await asyncio.sleep(settings.EDGAR_RATE_LIMIT_DELAY)
        response = await self._client.get(url, headers=self.base_headers)
        response.raise_for_status()
        return response

    # ─── Método principal: ATOM feed ──────────────────────────────────────────
    # Usamos el ATOM feed oficial — más estable que la API de búsqueda interna

    async def get_recent_form4_filings(self, start: int = 0, count: int = 100) -> list[dict]:
        """
        Una página del feed ATOM oficial de EDGAR, de más reciente a más antiguo.
        `start` permite paginar (0, 100, 200…); EDGAR admite como máximo 100 por página.
        """
        url = (
            f"{settings.EDGAR_BASE_URL}/cgi-bin/browse-edgar"
            f"?action=getcurrent&type=4&dateb=&owner=include"
            f"&count={count}&start={start}&search_text=&output=atom"
        )
        logger.info(f"Fetching Form 4 feed page start={start}")
        response = await self._get(url)
        return self._parse_atom_feed(response.text)

    # ─── Índice diario: todos los Form 4 de un día ────────────────────────────

    async def get_daily_form4_filings(self, day: date) -> list[dict] | None:
        """
        Todos los Form 4 presentados en `day`, desde el índice diario de EDGAR.
        Devuelve None si el índice no existe (fin de semana, festivo o aún no publicado).
        """
        quarter = (day.month - 1) // 3 + 1
        url = (
            f"{settings.EDGAR_BASE_URL}/Archives/edgar/daily-index/"
            f"{day.year}/QTR{quarter}/form.{day:%Y%m%d}.idx"
        )
        await asyncio.sleep(settings.EDGAR_RATE_LIMIT_DELAY)
        response = await self._client.get(url, headers=self.base_headers)
        if response.status_code in (403, 404):
            return None
        response.raise_for_status()

        filings = {}
        for line in response.text.splitlines():
            # "4   COMPANY NAME   CIK   YYYYMMDD   edgar/data/CIK/0001234567-26-000001.txt"
            if not line.startswith("4 "):
                continue
            m = re.search(r"\s(\d+)\s+(\d{8})\s+edgar/data/(\d+)/([\d-]+)\.txt\s*$", line)
            if not m:
                continue
            _, filed, path_cik, accession = m.groups()
            if accession in filings:
                continue  # cada filing aparece una vez por emisor y otra por insider
            filings[accession] = {
                "index_url": (
                    f"{settings.EDGAR_BASE_URL}/Archives/edgar/data/"
                    f"{path_cik}/{accession.replace('-', '')}/{accession}-index.htm"
                ),
                "accession_number": accession,
                "cik": path_cik.zfill(10),
                "filed_date": f"{filed[:4]}-{filed[4:6]}-{filed[6:]}",
            }
        logger.info(f"Daily index {day}: {len(filings)} Form 4 filings")
        return list(filings.values())

    def _parse_atom_feed(self, xml_text: str) -> list[dict]:
        """
        Parsea el feed ATOM de EDGAR.
        Cada entry tiene:
          - <link href="..."> → URL del índice del filing
          - <title> → "4 - COMPANY NAME (CIK) (Filer)"
          - SEC custom elements para company-name, cik, filing-date
        """
        from lxml import etree

        root = etree.fromstring(xml_text.encode())

        # Función para buscar por nombre local ignorando namespace
        def by_local(elem, name):
            return next(
                (el for el in elem.iter() if el.tag.split("}")[-1] == name),
                None
            )

        entries = [el for el in root.iter() if el.tag.split("}")[-1] == "entry"]
        filings = []
        seen_accessions = set()

        for entry in entries:
            # Extraer URL del índice desde <link>
            link_el = by_local(entry, "link")
            index_url = None
            if link_el is not None:
                index_url = link_el.get("href") or (link_el.text or "").strip()

            if not index_url:
                continue

            # Asegurarnos de que termina en -index.htm
            if not ("-index.htm" in index_url):
                continue

            # Intentar obtener datos del insider
            company_name_el = by_local(entry, "company-name")
            cik_el = by_local(entry, "cik")
            filed_date_el = by_local(entry, "filing-date")

            # Fallback: extraer CIK del título "4 - COMPANY (0001234567) (Filer)"
            company_name = "Unknown"
            cik = None
            if company_name_el is not None and company_name_el.text:
                company_name = company_name_el.text.strip()
            else:
                title_el = by_local(entry, "title")
                if title_el is not None and title_el.text:
                    m = re.search(r"4 - (.+?) \((\d+)\)", title_el.text)
                    if m:
                        company_name = m.group(1).strip()
                        cik = m.group(2).zfill(10)

            if cik is None and cik_el is not None and cik_el.text:
                cik = cik_el.text.strip().zfill(10)

            filed_date = filed_date_el.text.strip() if filed_date_el is not None and filed_date_el.text else ""

            # Cada filing aparece dos veces (emisor e insider): nos quedamos con una
            accession = self.accession_from_index_url(index_url)
            if accession in seen_accessions:
                continue
            seen_accessions.add(accession)

            filings.append({
                "index_url": index_url,
                "accession_number": accession,
                "company_name": company_name,
                "cik": cik,
                "filed_date": filed_date,
            })

        logger.info(f"Parsed {len(filings)} filings from ATOM feed")
        return filings

    # ─── Descarga del XML del Form 4 ─────────────────────────────────────────

    async def get_form4_xml(self, index_url: str) -> tuple[str, str] | None:
        """
        Dado el URL del índice de un filing, descarga el XML puro del Form 4.
        El índice es una página HTML con tabla de documentos.
        Devuelve (xml_content, accession_number), o None si el filing no tiene
        un XML de Form 4. Los errores HTTP se propagan para reintentarlo más tarde.
        """
        response = await self._get(index_url)
        xml_url, accession = self._find_xml_in_index(response.text, index_url)

        if not xml_url:
            logger.warning(f"No XML found in index: {index_url}")
            return None

        logger.debug(f"Downloading XML: {xml_url}")
        xml_response = await self._get(xml_url)

        content = xml_response.text.strip()
        # Verificar que es XML real, no HTML
        if content.startswith("<!DOCTYPE") or content.startswith("<html"):
            logger.warning(f"Got HTML instead of XML from {xml_url}")
            return None

        return content, accession

    @staticmethod
    def accession_from_index_url(index_url: str) -> str:
        """.../0001234567-25-000001-index.htm → 0001234567-25-000001"""
        m = re.search(r"/([\d\-]+)-index\.htm", index_url)
        return m.group(1) if m else ""

    def _find_xml_in_index(self, index_html: str, index_url: str) -> tuple[str | None, str]:
        """
        Parsea el índice HTML del filing y encuentra el XML puro del Form 4.

        La página de índice tiene una tabla como:
          Type | Document               | Description
          4    | 0001234567-25-000001.xml | Primary document
          4    | form4.xml              | ...
        
        Buscamos el documento con Type="4" que sea .xml (no .htm).
        """
        from lxml import etree

        # Extraer accession number de la URL
        # Ej: /Archives/edgar/data/123/000123456725000001/0001234567-25-000001-index.htm
        accession = self.accession_from_index_url(index_url)

        try:
            root = etree.fromstring(index_html.encode(), etree.HTMLParser())
            rows = root.findall(".//tr")

            candidates = []
            for row in rows:
                cells = row.findall(".//td")
                if len(cells) < 2:
                    continue

                # La tabla del índice de EDGAR tiene columnas: Seq | Type | Document | Type | Size
                # El link está en cualquier celda — buscamos en todas
                all_links = row.findall(".//a")
                for link in all_links:
                    href = link.get("href", "")
                    if not href.endswith(".xml"):
                        continue
                    # El tipo "4" puede estar en cells[1] o cells[3]
                    cell_texts = [(c.text or "").strip() for c in cells]
                    is_form4_type = "4" in cell_texts
                    if is_form4_type:
                        candidates.append(href)
                    elif any(k in href.lower() for k in ("form4", "primary", "ownership", "doc4")):
                        candidates.append(href)

            if not candidates:
                # Último recurso: cualquier .xml que no sea stylesheet/schema
                for a in root.findall(".//a"):
                    href = a.get("href", "")
                    if href.endswith(".xml") and not any(
                        k in href.lower() for k in ("xsd", "stylesheet", "schema", "xslt")
                    ):
                        candidates.append(href)

            # CRÍTICO: excluir links XSLT — están en carpeta xslF345X06
            # y devuelven HTML aunque tengan extensión .xml
            candidates = [h for h in candidates if "xsl" not in h.lower()]

            if candidates:
                href = candidates[0]
                base = settings.EDGAR_BASE_URL
                full_url = f"{base}{href}" if href.startswith("/") else href
                return full_url, accession

        except Exception as e:
            logger.error(f"Error parsing index: {e}")

        return None, accession

    # ─── Búsqueda por CIK ────────────────────────────────────────────────────

    async def get_filings_by_cik(self, cik: str, days_back: int = 90) -> list[dict]:
        """Obtiene el historial de Form 4 para una empresa por su CIK."""
        padded_cik = cik.zfill(10)
        url = f"{settings.EDGAR_DATA_URL}/submissions/CIK{padded_cik}.json"

        response = await self._get(url)
        data = response.json()

        filings = []
        recent = data.get("filings", {}).get("recent", {})
        cutoff = datetime.now() - timedelta(days=days_back)

        for form, filing_date_str, accession in zip(
            recent.get("form", []),
            recent.get("filingDate", []),
            recent.get("accessionNumber", []),
        ):
            if form != "4":
                continue
            try:
                if datetime.strptime(filing_date_str, "%Y-%m-%d") < cutoff:
                    continue
            except ValueError:
                continue

            acc_formatted = accession.replace("-", "")
            index_url = (
                f"{settings.EDGAR_BASE_URL}/Archives/edgar/data/"
                f"{cik.lstrip('0')}/{acc_formatted}/{accession}-index.htm"
            )
            filings.append({
                "index_url": index_url,
                "accession_number": accession,
                "filed_date": filing_date_str,
                "cik": padded_cik,
            })

        return filings

    async def search_company_cik(self, ticker: str) -> str | None:
        """Busca el CIK de una empresa por su ticker."""
        try:
            response = await self._get(
                f"{settings.EDGAR_BASE_URL}/cgi-bin/browse-edgar"
                f"?company=&CIK={ticker}&type=4&dateb=&owner=include"
                f"&count=1&action=getcompany&output=atom"
            )
            from lxml import etree
            root = etree.fromstring(response.text.encode())
            cik_el = next(
                (el for el in root.iter() if el.tag.split("}")[-1] == "cik"),
                None,
            )
            return cik_el.text.zfill(10) if cik_el is not None else None
        except Exception as e:
            logger.error(f"Error searching CIK for {ticker}: {e}")
            return None