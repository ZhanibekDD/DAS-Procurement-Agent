from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from openpyxl import load_workbook

from .models import SupplierCreate
from .upload_io import open_payload, payload_sha256, MAX_FILE, UploadTooLarge, TOO_LARGE


HEADER_ALIASES = {
    "name": {"наименование", "поставщик", "контрагент", "компания", "name", "supplier"},
    "tax_id": {"инн", "бин", "иин", "огрн", "tax_id", "tax id"},
    "region": {"регион", "город", "область", "region", "city"},
    "email": {"email", "e-mail", "электронная почта", "почта"},
    "phone": {"телефон", "phone", "мобильный"},
    "telegram": {"telegram", "телеграм", "tg"},
    "max_contact": {"max", "мах", "макс", "max контакт", "мах контакт"},
    "cluster": {"кластер", "cluster"},
    "categories": {"категория", "категории", "товары", "услуги", "category"},
    "rating": {"рейтинг", "rating", "оценка"},
    "verified": {"проверен", "проверенный", "verified"},
}


@dataclass
class ImportPreview:
    rows: list[SupplierCreate] = field(default_factory=list)
    errors: list[dict[str, object]] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)


def _normalize(value: object) -> str:
    return " ".join(str(value or "").strip().lower().replace("ё", "е").split())


def _header_mapping(headers: Iterable[object]) -> tuple[list[str], dict[int, str]]:
    originals = [str(value or "").strip() for value in headers]
    mapping: dict[int, str] = {}
    for index, header in enumerate(originals):
        normalized = _normalize(header)
        for field_name, aliases in HEADER_ALIASES.items():
            if normalized in aliases:
                mapping[index] = field_name
                break
    if "name" not in mapping.values():
        raise ValueError("supplier table must contain a supplier/name column")
    if "region" not in mapping.values():
        raise ValueError("supplier table must contain a region/city column")
    return originals, mapping


def _bool(value: object) -> bool:
    return _normalize(value) in {"1", "true", "yes", "да", "проверен", "проверенный"}


def _supplier_from_row(row: list[object], mapping: dict[int, str]) -> SupplierCreate | None:
    values = {field_name: row[index] if index < len(row) else "" for index, field_name in mapping.items()}
    if not any(str(value or "").strip() for value in values.values()):
        return None
    raw_categories = str(values.get("categories") or "")
    categories = [part.strip() for part in raw_categories.replace(";", ",").split(",") if part.strip()]
    raw_rating = str(values.get("rating") or "").replace(",", ".").strip()
    return SupplierCreate(
        name=str(values.get("name") or ""),
        tax_id=str(values.get("tax_id") or ""),
        region=str(values.get("region") or ""),
        email=str(values.get("email") or ""),
        phone=str(values.get("phone") or ""),
        telegram=str(values.get("telegram") or ""),
        max_contact=str(values.get("max_contact") or ""),
        cluster=str(values.get("cluster") or ""),
        categories=categories,
        rating=float(raw_rating) if raw_rating else 3.0,
        verified=_bool(values.get("verified")),
    )


def _parse_rows(rows: Iterable[tuple[object, ...]]) -> ImportPreview:
    iterator = iter(rows)
    try:
        header_row = next(iterator)
    except StopIteration as exc:
        raise ValueError("supplier table is empty") from exc
    headers, mapping = _header_mapping(header_row)
    preview = ImportPreview(headers=headers)
    for row_number, row in enumerate(iterator, start=2):
        if row_number>10001 or len(row)>100 or any(len(str(value or ''))>8000 for value in row):
            raise ValueError('Supplier table exceeds safe row/column/cell limits')
        try:
            supplier = _supplier_from_row(list(row), mapping)
            if supplier is not None:
                preview.rows.append(supplier)
        except Exception as exc:
            preview.errors.append({"row": row_number, "error": str(exc)})
    return preview


def parse_supplier_table(content: bytes, filename: str) -> ImportPreview:
    from .table_ingest import safe_upload, validate_xlsx_expansion
    safe_upload(content,filename,{'.csv','.xlsx'})
    with open_payload(content) as stream:
        return _parse_supplier_stream(content,stream,filename)


def _parse_supplier_stream(content,stream,filename):
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        text = io.TextIOWrapper(stream,encoding='utf-8-sig',newline='')
        sample = text.read(4096)
        text.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
            dialect.delimiter = ";"
        return _parse_rows(tuple(row) for row in csv.reader(text, dialect))
    if suffix == ".xlsx":
        from .table_ingest import validate_xlsx_expansion
        validate_xlsx_expansion(content,filename)
        workbook = load_workbook(stream, read_only=True, data_only=True)
        try:
            sheet = workbook.active
            return _parse_rows(sheet.iter_rows(values_only=True))
        finally:
            workbook.close()
    raise ValueError("only .csv and .xlsx supplier tables are supported")


# ── PR #8: price-list / КП / счёт batch extraction ───────────────────────────

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date as _date
from decimal import Decimal, InvalidOperation


# ---------------------------------------------------------------------------
# Cluster detection by region keyword
# ---------------------------------------------------------------------------
def detect_cluster(region: str) -> tuple[str, str]:
    """Return (cluster, cluster_status) for a region string.

    Delegates to regions.py as the single source of truth.
    Only 'cluster_1' / 'cluster_2' are valid output values.
    Unknown regions return ('', 'needs_review') — no guessing.
    """
    from .region_routing import infer_cluster as _infer_cluster
    cluster = _infer_cluster(region)
    return cluster, ('confirmed' if cluster else 'needs_review')


# ---------------------------------------------------------------------------
# Deduplication key
# ---------------------------------------------------------------------------
def supplier_dedup_key(tax_id: str, name: str, email: str, phone: str) -> str:
    """Stable deduplication key.

    If a valid ИНН is provided the key is INN-only: changing email / phone
    does NOT create a new supplier (contact updates trigger review, not
    a duplicate entry).

    Without ИНН: normalized name + first available contact.
    """
    normalized_inn = re.sub(r'[^0-9]', '', tax_id)
    if normalized_inn:
        raw = normalized_inn
    else:
        normalized_name = re.sub(r'[^а-яa-z0-9]+', ' ', name.casefold()).strip()
        contact = email.casefold().strip() or re.sub(r'[^0-9]', '', phone)
        raw = normalized_name + '|' + contact
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Currency normalisation
# ---------------------------------------------------------------------------
_CURRENCY_MAP = {
    'руб': 'RUB', 'rub': 'RUB', 'rur': 'RUB', 'р.': 'RUB',
    'usd': 'USD', 'доллар': 'USD', '$': 'USD',
    'eur': 'EUR', 'евро': 'EUR', '€': 'EUR',
    'kzt': 'KZT', 'тенге': 'KZT', '₸': 'KZT',
}

_CURRENCY_PATTERNS = {
    'RUB': r'(?<!\w)(?:rub|rur|руб(?:ль|ля|лей)?\.?)(?!\w)|₽',
    'USD': r'(?<!\w)(?:usd|доллар(?:ов|а)?)(?!\w)|\$',
    'EUR': r'(?<!\w)(?:eur|евро)(?!\w)|€',
    'KZT': r'(?<!\w)(?:kzt|тенге)(?!\w)|₸',
}


def _explicit_currencies(text: str) -> set[str]:
    return {code for code, pattern in _CURRENCY_PATTERNS.items()
            if re.search(pattern, text, re.I)}


def _vat_context(text: str) -> bool | None:
    without = bool(re.search(r'\bбез\s+ндс\b', text, re.I))
    included = bool(re.search(r'\b(?:с\s+ндс|включая\s+ндс|ндс\s+включ[её]н)\b', text, re.I))
    if without and included:
        raise ValueError('conflicting VAT declarations require review')
    return False if without else True if included else None


def _decimal_price(value: str) -> str:
    """Keep currency amounts decimal and reject signs/text instead of stripping them."""
    normalized = re.sub(r'[\s\u00a0\u202f]', '', value)
    if not re.fullmatch(r'\d+(?:[.,]\d+)?', normalized):
        raise ValueError('invalid or ambiguous price requires review')
    try:
        amount = Decimal(normalized.replace(',', '.'))
    except InvalidOperation as exc:
        raise ValueError('invalid price requires review') from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError('price must be positive')
    return format(amount, 'f')


def detect_currency(text: str) -> str:
    t = text.casefold()
    for k, v in _CURRENCY_MAP.items():
        if k in t:
            return v
    return 'RUB'


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(
    r'(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})'
    r'|(\d{4})-(\d{2})-(\d{2})'
)


def extract_date(text: str) -> str | None:
    m = _DATE_RE.search(text)
    if not m:
        return None
    if m.group(4):            # YYYY-MM-DD
        y, mo, d = m.group(4), m.group(5), m.group(6)
    else:                     # D.M.Y or D/M/Y
        d, mo, y = m.group(1), m.group(2), m.group(3)
    if len(y) == 2:
        y = '20' + y
    try:
        return _date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None


def price_validity_state(valid_until: str | None) -> str:
    """Tristate validity for a price entry.

    * 'active'  — valid_until is today or in the future; may be used as current price
    * 'expired' — valid_until is in the past
    * 'unknown' — valid_until is None or unparseable; NOT treated as active
    """
    if not valid_until:
        return 'unknown'
    try:
        if _date.fromisoformat(valid_until) >= _date.today():
            return 'active'
        return 'expired'
    except ValueError:
        return 'unknown'


def is_price_expired(valid_until: str | None) -> bool:
    """Legacy helper kept for backward-compat tests. Use price_validity_state instead."""
    if not valid_until:
        return False
    try:
        return _date.fromisoformat(valid_until) < _date.today()
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Column detection helpers
# ---------------------------------------------------------------------------
_PRICE_COL_NAMES: set[str] = {
    'price', 'цена', 'стоимость', 'цена с ндс', 'цена без ндс',
    'прайс', 'за ед', 'unit price', 'unit_price', 'цена ед', 'цена/ед',
}
_NAME_COL_NAMES: set[str] = {
    'наименование', 'название', 'товар', 'позиция', 'item', 'name', 'description',
    'наим.', 'описание', 'продукция', 'материал', 'номенклатура',
}
_QTY_COL_NAMES: set[str] = {
    'кол-во', 'количество', 'qty', 'quantity', 'кол.', 'объём', 'объем', 'кол',
}
_UNIT_COL_NAMES: set[str] = {
    'ед.изм', 'ед. изм', 'единица', 'unit', 'ед', 'uom', 'ед.изм.',
}


def _col_index(headers: list[str], names: set[str]) -> int | None:
    for i, h in enumerate(headers):
        if h.casefold().strip() in names:
            return i
    for i, h in enumerate(headers):
        hcf = h.casefold().strip()
        if any(n in hcf for n in names):
            return i
    return None


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------
@dataclass
class ExtractedItem:
    item_name: str
    normalized_name: str
    brand: str
    quantity: str
    unit: str
    unit_price: str
    total_price: str
    currency: str
    vat_included: bool
    source_page: int | None
    source_sheet: str
    source_row: int | None
    source_cell: str
    source_text: str
    review_warning: str = ''


@dataclass
class DocumentExtractResult:
    filename: str
    sha256: str
    document_type: str        # price_list | invoice | commercial_offer | unknown
    supplier_name: str
    supplier_tax_id: str
    supplier_region: str
    supplier_email: str
    supplier_phone: str
    supplier_contact: str
    document_date: str | None
    valid_until: str | None
    currency: str
    vat_included: bool
    items: list[ExtractedItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Document type classifier
# ---------------------------------------------------------------------------
def _classify_document(text: str) -> str:
    tl = text.casefold()
    if any(w in tl for w in ('счёт-фактура', 'счет-фактура', 'упд')):
        return 'invoice'
    if any(w in tl for w in ('коммерческое предложение', 'кп №', 'кп от')):
        return 'commercial_offer'
    if any(w in tl for w in ('прайс-лист', 'price list', 'прайс лист', 'прайслист')):
        return 'price_list'
    if any(w in tl for w in ('счёт №', 'счет №', 'счёт на', 'счет на')):
        return 'invoice'
    if any(w in tl for w in ('предложение', 'спецификация')):
        return 'commercial_offer'
    return 'unknown'


# supplier regex patterns
_SUPPLIER_INN_RE = re.compile(r'ИНН[:\s]+([0-9]{10,12})', re.I)
_SUPPLIER_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
_SUPPLIER_PHONE_RE = re.compile(r'(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}')
_ORG_RE = re.compile(r'(?:ООО|ИП|АО|ЗАО|ПАО)[\s"«]+([^»"\n]{3,80})', re.I)


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------
MAX_PDF_PAGES = 50
MAX_PDF_TEXT = 1_000_000
from threading import BoundedSemaphore
_PDF_SLOTS = BoundedSemaphore(2)


def _pdf_worker(content: bytes, filename: str, pipe) -> None:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    except ImportError:
        pass  # Parent wall-clock deadline applies on Windows too.
    try:
        pipe.send(extract_from_pdf(content, filename))
    except BaseException:
        pipe.send(None)
    finally:
        pipe.close()


def _extract_pdf_isolated(content: bytes, filename: str) -> DocumentExtractResult:
    """Untrusted PDF parsers cannot exhaust the HTTP worker or run forever."""
    import multiprocessing
    context = multiprocessing.get_context('spawn')
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_pdf_worker, args=(content, filename, sender))
    if not _PDF_SLOTS.acquire(blocking=False):
        receiver.close();sender.close()
        raise ValueError('PDF parser capacity is busy; retry later')
    result = None
    try:
        process.start();sender.close()
        if receiver.poll(20):
            try:
                result = receiver.recv()
            except EOFError:
                pass
    finally:
        receiver.close()
        if process.pid is not None:
            process.join(timeout=1)
            if process.is_alive():
                process.kill();process.join(timeout=2)
            process.close()
        sender.close()
        _PDF_SLOTS.release()
    if result is None:
        raise ValueError('PDF extraction exceeded resource limits or failed; no extracted data accepted')
    return result


def _extract_pdf_text(content: bytes) -> tuple[list[str], list[str]]:
    """Return (page_texts, errors)."""
    try:
        from pypdf import PdfReader
        import io as _io
        with open_payload(content) as stream:
            reader = PdfReader(stream)
            if reader.is_encrypted or len(reader.pages) > MAX_PDF_PAGES:
                raise ValueError('PDF exceeds 50 pages or is encrypted')
            pages = []
            total = 0
            for page in reader.pages:
                text = page.extract_text() or ''
                total += len(text)
                if len(text) > 100000 or total > MAX_PDF_TEXT:
                    raise ValueError('PDF expanded text exceeds limits')
                pages.append(text)
            return pages, []
    except Exception as exc:
        return [], [f'PDF parse error: {exc}']


def _pdf_price_without_suffix(value: str, currency: str) -> str:
    value = value.strip()
    if currency == 'RUB':
        value = re.sub(r'\s*(?:руб\.?|₽|RUB)\s*$', '', value, flags=re.I)
    return value


def _pdf_price(value: str, currency: str) -> str:
    """Parse an explicit price cell, not arbitrary numbers in a PDF row."""
    value = _pdf_price_without_suffix(value, currency)
    value = re.sub(r'[\s\u00a0\u202f]', '', value)
    if re.fullmatch(r'\d{1,3}(?:,\d{3})+\.\d{2}', value):
        value = value.replace(',', '')
    elif re.fullmatch(r'\d{1,3}(?:\.\d{3})+,\d{2}', value):
        value = value.replace('.', '')
    return _decimal_price(value)


def _reviewable_pdf_price(value: str, currency: str) -> tuple[str, str]:
    # An explicitly negotiated price is not zero and not a parseable amount.
    # Retain the row for review without weakening numeric financial validation.
    label = ' '.join(_pdf_price_without_suffix(value, currency).casefold().split()).strip(' .')
    if re.fullmatch(r'(?:договорная(?: цена)?|цена договорная|(?:цена |стоимость )?по (?:запросу|согласованию)|уточняйте(?: цену)?)', label):
        return '', f'Цена «{value.strip()}» не указана числом. Уточните цену или исключите строку перед импортом.'
    return _pdf_price(value, currency), ''


def _price_table_header(table):
    """Join adjacent header tiers by column; never absorb a priced data row."""
    combined = []
    for ri, row in enumerate(table[:3]):
        cells = [str(c or '').strip() for c in (row or [])]
        if not cells:
            continue
        # A merged report title is not a header tier. In particular, its
        # "прайс" label must never turn an article column into a price column.
        if not combined and (sum(bool(c) for c in cells) < 2
                             or _col_index(cells, _NAME_COL_NAMES) is None):
            continue
        if combined and len(cells) != len(combined):
            break
        if not combined:
            combined = [''] * len(cells)
        # A two-tier heading has empty/label cells, not a numeric price.
        if ri and any(re.search(r'\d', c) for c in cells):
            break
        # A complete explicit header takes precedence over preceding tiers.
        # Merge only genuinely split headings, retaining their column geometry.
        row_nc, row_pc = _col_index(cells, _NAME_COL_NAMES), _col_index(cells, _PRICE_COL_NAMES)
        if row_nc is not None and row_pc is not None and row_nc != row_pc:
            combined = cells
        else:
            combined = [' '.join(filter(None, (a, b))) for a, b in zip(combined, cells)]
        nc, pc = _col_index(combined, _NAME_COL_NAMES), _col_index(combined, _PRICE_COL_NAMES)
        if nc is not None and pc is not None and nc != pc:
            quantities = ['' if re.search(r'\b(?:масса|вес)\b|\b(?:м3|м³|кг|kg)\b', label, re.I) else label
                          for label in combined]
            return ri, (nc, pc, _col_index(quantities, _QTY_COL_NAMES), _col_index(combined, _UNIT_COL_NAMES))
    return None


def _extract_items_from_pdf_tables(
    content: bytes, currency: str, vat_included: bool
) -> tuple[list[ExtractedItem], bool]:
    """Returns (items, has_structured_tables).
    has_structured_tables=True means pdfplumber found at least one table,
    even if no price column was present.  Caller uses this to suppress the
    text-regex fallback for structural docs (engineering specs, drawings).
    """
    """Primary PDF item extractor — uses pdfplumber structured table API.

    Extracts rows only from tables that have a recognisable header with both
    a name column (_NAME_COL_NAMES) and a price column (_PRICE_COL_NAMES).
    Engineering specs (Масса ед., кг — no price column) correctly return [].
    Falls back silently to [] if pdfplumber is not installed.
    """
    items: list[ExtractedItem] = []
    has_tables = False  # True once pdfplumber finds any non-trivial table
    continuation = None
    try:
        import pdfplumber
        import io as _io_plumb
        with open_payload(content) as stream, pdfplumber.open(stream) as pdf:
            if len(pdf.pages) > MAX_PDF_PAGES:
                raise ValueError('PDF exceeds 50 pages')
            for page_num, page in enumerate(pdf.pages, start=1):
                if len(page.chars) > 100000:
                    raise ValueError('PDF page complexity exceeds limits')
                table_objects = page.find_tables()
                tables = [t.extract() for t in table_objects]
                page.close()
                if len(tables) > 100:
                    raise ValueError('PDF page complexity exceeds limits')
                for table_index, table in enumerate(tables):
                    if not table or len(table) < 2:
                        continue
                    has_tables = True  # at least one real table found
                    geometry = tuple(round(c.bbox[0], 1) for c in table_objects[table_index].columns)
                    header = _price_table_header(table)
                    if header:
                        header_row_idx, columns = header
                    elif (continuation and table_index == 0 and continuation[0] == page_num - 1
                          and len(geometry) == len(continuation[1])
                          and all(abs(a-b) <= 2 for a,b in zip(geometry,continuation[1]))):
                        columns = continuation[2]
                        nc, pc, _, _ = columns
                        first = table[0]
                        # Only a same-layout adjacent page beginning with an actual
                        # priced row can inherit the already verified table header.
                        try:
                            assert len(first) == len(geometry) and first[nc]
                            _reviewable_pdf_price(str(first[pc] or ''), currency)
                        except (ValueError, AssertionError, IndexError):
                            continuation = None
                            continue
                        header_row_idx = -1
                    else:
                        continuation = None
                        continue
                    name_col, price_col, qty_col, unit_col = columns
                    continuation = (page_num, geometry, columns)
                    for row_num, row in enumerate(
                        table[header_row_idx + 1:], start=header_row_idx + 2
                    ):
                        if row is None:
                            continue
                        if row_num > 10000 or len(row) > 100 or len(items) >= 10000:
                            raise ValueError('PDF table exceeds row/column limits')
                        raw_name = (
                            str(row[name_col] or '').strip()
                            if name_col < len(row) else ''
                        )
                        raw_price = (
                            str(row[price_col] or '').strip()
                            if price_col < len(row) else ''
                        )
                        if not raw_name or not raw_price:
                            continue
                        # Skip numbering rows (e.g. "1 2 3 4 5 6 7" row in Russian invoices)
                        if raw_name.isdigit():
                            continue
                        # Normalise price — remove thousands separators, convert comma to dot
                        price_clean, review_warning = _reviewable_pdf_price(raw_price, currency)
                        raw_qty = ''
                        if qty_col is not None and qty_col < len(row):
                            raw_qty = str(row[qty_col] or '').strip()
                        raw_unit = 'шт.'  # шт.
                        if unit_col is not None and unit_col < len(row):
                            u = str(row[unit_col] or '').strip()
                            if u:
                                raw_unit = u
                        item_name = raw_name.replace(chr(10), ' ').strip()[:200]
                        norm = re.sub(r'[^а-яa-z0-9 ]+', ' ', item_name.casefold()).strip()
                        source_text = ' | '.join(str(c or '') for c in row if c)[:300]
                        items.append(ExtractedItem(
                            item_name=item_name,
                            normalized_name=norm,
                            brand='',
                            quantity=raw_qty,
                            unit=raw_unit,
                            unit_price=price_clean,
                            total_price='',
                            currency=currency,
                            vat_included=vat_included,
                            source_page=page_num,
                            source_sheet='',
                            source_row=row_num,
                            source_cell='',
                            source_text=source_text,
                            review_warning=review_warning,
                        ))
    except ImportError:
        pass  # pdfplumber not available — caller falls back to text method
    return items, has_tables


def _extract_items_from_pdf_text(
    pages: list[str], currency: str, vat_included: bool
) -> list[ExtractedItem]:
    """Fallback PDF extractor — line-by-line regex for unstructured text PDFs."""
    items: list[ExtractedItem] = []
    # Matches: <name 5-80 chars> <qty digits> <price with 2 decimal places>
    price_re = re.compile(
        r'^(.{5,80})\s+(\d[\d\s.,]{0,14})\s+(\d[\d\s.,]*[.,]\d{2})\s*$'
    )
    for page_num, text in enumerate(pages, start=1):
        for row_num, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if len(line) < 10:
                continue
            m = price_re.match(line)
            if not m:
                continue
            name_raw = m.group(1).strip()
            qty_raw = m.group(2).strip()
            price_raw = (
                m.group(3)
                .replace(' ', '')
                .replace(' ', '')
                .replace(',', '.')
            )
            price_raw = _decimal_price(price_raw)
            norm = re.sub(r'[^а-яa-z0-9 ]+', ' ', name_raw.casefold()).strip()
            items.append(ExtractedItem(
                item_name=name_raw,
                normalized_name=norm,
                brand='',
                quantity=qty_raw,
                unit='шт.',
                unit_price=price_raw,
                total_price='',
                currency=currency,
                vat_included=vat_included,
                source_page=page_num,
                source_sheet='',
                source_row=row_num,
                source_cell='',
                source_text=line,
            ))
    return items


def extract_from_pdf(content: bytes, filename: str) -> DocumentExtractResult:
    sha256 = payload_sha256(content)
    pages, errors = _extract_pdf_text(content)
    if errors:
        return DocumentExtractResult(filename=filename,sha256=sha256,document_type='unknown',
            supplier_name='',supplier_tax_id='',supplier_region='',supplier_email='',supplier_phone='',supplier_contact='',
            document_date=None,valid_until=None,currency='',vat_included=False,items=[],errors=errors)
    full_text = '\n'.join(pages)
    header_text = '\n'.join(pages[:2]) if pages else ''

    doc_type = _classify_document(full_text)
    currencies = _explicit_currencies(full_text)
    currency = next(iter(currencies)) if len(currencies) == 1 else ''
    if not currency:
        errors.append('PDF missing or ambiguous currency requires review')
    try:
        explicit_vat = _vat_context(full_text)
        if explicit_vat is None:
            errors.append('PDF missing VAT basis requires review')
    except ValueError as exc:
        explicit_vat = None
        errors.append(f'PDF: {exc}')
    # No financial row can carry an inferred basis. False is only an unused
    # placeholder in the rejected result; errors prevent any row extraction.
    vat_included = explicit_vat is True
    doc_date = extract_date(full_text)

    valid_re = re.compile(
        r'(?:действует\s+до'
        r'|действительно\s+до'
        r'|срок\s+действия)'
        r'[^\d]*(\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4})', re.I
    )
    valid_m = valid_re.search(full_text)
    valid_until = extract_date(valid_m.group(1)) if valid_m else None

    tax_id_m = _SUPPLIER_INN_RE.search(header_text)
    email_m = _SUPPLIER_EMAIL_RE.search(header_text)
    phone_m = _SUPPLIER_PHONE_RE.search(header_text)
    org_m = _ORG_RE.search(header_text)
    supplier_name = org_m.group(0).strip()[:120] if org_m else ''

    # Primary: structured table extraction (pdfplumber) — handles invoices, КП, price-lists
    items, _has_tables = ([], True) if errors else _extract_items_from_pdf_tables(content, currency, vat_included)
    # Fallback: line-by-line regex — ONLY for PDFs with no tables at all.
    # If pdfplumber found tables but no price column → structural doc (engineering specs,
    # drawings) → return 0 items; do NOT mine masses/quantities as fake prices.
    if not items and not _has_tables:
        items = _extract_items_from_pdf_text(pages, currency, vat_included)

    errors.extend(f'Страница {item.source_page}, строка {item.source_row}: {item.review_warning}'
                  for item in items if item.review_warning)

    return DocumentExtractResult(
        filename=filename,
        sha256=sha256,
        document_type=doc_type,
        supplier_name=supplier_name,
        supplier_tax_id=tax_id_m.group(1) if tax_id_m else '',
        supplier_region='',
        supplier_email=email_m.group(0) if email_m else '',
        supplier_phone=phone_m.group(0) if phone_m else '',
        supplier_contact='',
        document_date=doc_date,
        valid_until=valid_until,
        currency=currency,
        vat_included=vat_included,
        items=items,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# XLSX extraction
# ---------------------------------------------------------------------------
def extract_from_xlsx(content: bytes, filename: str) -> DocumentExtractResult:
    from openpyxl import load_workbook
    import io as _io

    sha256 = payload_sha256(content)
    errors: list[str] = []

    stream = open_payload(content)
    try:
        from .table_ingest import MAX_ROWS, MAX_COLS, validate_xlsx_expansion
        # Legacy batch uploads normalize the storage basename; inspect the same
        # safe basename here, while preserving provenance in the result.
        validate_xlsx_expansion(content, Path(filename.replace('\\', '/')).name)
        wb = load_workbook(stream, read_only=True, data_only=True, keep_links=False)
        for ws in wb:
            if (ws.max_row or 0) > MAX_ROWS + 100 or (ws.max_column or 0) > MAX_COLS:
                wb.close()
                raise ValueError('XLSX row/column dimensions exceed limits')
    except Exception as exc:
        stream.close()
        return DocumentExtractResult(
            filename=filename, sha256=sha256, document_type='unknown',
            supplier_name='', supplier_tax_id='', supplier_region='',
            supplier_email='', supplier_phone='', supplier_contact='',
            document_date=None, valid_until=None, currency='RUB', vat_included=True,
            items=[], errors=[f'XLSX open error: {exc}'],
        )

    try:
        return _extract_bounded_xlsx(wb, filename, sha256)
    finally:
        wb.close()
        stream.close()


def _extract_bounded_xlsx(wb, filename: str, sha256: str) -> DocumentExtractResult:
    from .table_ingest import MAX_COLS
    errors: list[str] = []
    all_items: list[ExtractedItem] = []
    header_text_parts: list[str] = []
    sheets: list[tuple[str, int, list[str], str]] = []

    # A document title mentioning "price" is not itself a table header.
    for sheet_name in wb.sheetnames:
        header_idx: int | None = None
        headers: list[str] = []
        header_parts: list[str] = []
        for i, row in enumerate(wb[sheet_name].iter_rows(max_row=100, max_col=MAX_COLS, values_only=True)):
            header_parts.extend(str(c) for c in row if c is not None)
            row_strs = [str(c).strip() if c is not None else '' for c in row]
            if (_col_index(row_strs, _NAME_COL_NAMES) is not None
                    and _col_index(row_strs, _PRICE_COL_NAMES) is not None):
                header_idx = i
                headers = row_strs
                break
        if header_idx is None:
            errors.append(f'Sheet {sheet_name!r}: required columns not found')
            continue
        header_text = ' '.join(header_parts)
        header_text_parts.append(header_text)
        sheets.append((sheet_name, header_idx, headers, header_text))

    for sheet_name, header_idx, headers, sheet_header in sheets:
        sheet_currencies = _explicit_currencies(sheet_header)
        currencies = sheet_currencies
        if len(currencies) != 1:
            errors.append(f'Sheet {sheet_name!r}: missing or ambiguous currency requires review')
            continue
        sheet_currency = next(iter(currencies))
        try:
            # Do not inherit VAT across sheets: each price table is its own context.
            explicit_vat = _vat_context(sheet_header)
        except ValueError as exc:
            errors.append(f'Sheet {sheet_name!r}: {exc}')
            continue
        if explicit_vat is None:
            errors.append(f'Sheet {sheet_name!r}: missing VAT basis requires review')
            continue
        sheet_vat = explicit_vat

        name_col = _col_index(headers, _NAME_COL_NAMES)
        price_col = _col_index(headers, _PRICE_COL_NAMES)
        qty_col = _col_index(headers, _QTY_COL_NAMES)
        unit_col = _col_index(headers, _UNIT_COL_NAMES)

        if name_col is None or price_col is None:
            errors.append(f'Sheet {sheet_name!r}: required columns not found; headers={headers[:8]}')
            continue

        max_col = max(c for c in [name_col, price_col, qty_col, unit_col] if c is not None)

        for row_idx, row in enumerate(wb[sheet_name].iter_rows(
                min_row=header_idx + 2, max_col=MAX_COLS, values_only=True), start=header_idx + 2):
            cells = [str(c).strip() if c is not None else '' for c in row]
            if len(cells) <= max_col:
                continue
            name = cells[name_col]
            price = cells[price_col]
            qty = cells[qty_col] if qty_col is not None and qty_col < len(cells) else ''
            unit = cells[unit_col] if unit_col is not None and unit_col < len(cells) else ''
            if not name or not price:
                continue
            row_currencies = _explicit_currencies(' '.join(cells))
            if row_currencies and row_currencies != {sheet_currency}:
                errors.append(f'Sheet {sheet_name!r} row {row_idx}: currency conflicts with header')
                continue
            try:
                price_clean = _decimal_price(str(price))
            except ValueError as exc:
                errors.append(f'Sheet {sheet_name!r} row {row_idx}: {exc}')
                continue
            norm = re.sub(r'[^а-яa-z0-9 ]+', ' ', name.casefold()).strip()
            all_items.append(ExtractedItem(
                item_name=name,
                normalized_name=norm,
                brand='',
                quantity=qty,
                unit=unit,
                unit_price=price_clean,
                total_price='',
                currency=sheet_currency,
                vat_included=sheet_vat,
                source_page=None,
                source_sheet=sheet_name,
                source_row=row_idx,
                source_cell='',
                source_text='|'.join(cells[:8]),
            ))

    header_text = ' '.join(header_text_parts)
    doc_type = _classify_document(header_text)
    item_currencies = {item.currency for item in all_items}
    currency = (next(iter(item_currencies)) if len(item_currencies) == 1
                else 'MIXED' if len(item_currencies) > 1 else '')
    # Per-item values are authoritative; the document summary is not used for persistence.
    vat_included = all(item.vat_included for item in all_items) if all_items else False
    doc_date = extract_date(header_text)

    tax_id_m = _SUPPLIER_INN_RE.search(header_text)
    email_m = _SUPPLIER_EMAIL_RE.search(header_text)
    phone_m = _SUPPLIER_PHONE_RE.search(header_text)
    org_m = _ORG_RE.search(header_text)
    supplier_name = org_m.group(0).strip()[:120] if org_m else ''

    return DocumentExtractResult(
        filename=filename,
        sha256=sha256,
        document_type=doc_type,
        supplier_name=supplier_name,
        supplier_tax_id=tax_id_m.group(1) if tax_id_m else '',
        supplier_region='',
        supplier_email=email_m.group(0) if email_m else '',
        supplier_phone=phone_m.group(0) if phone_m else '',
        supplier_contact='',
        document_date=doc_date,
        valid_until=None,
        currency=currency,
        vat_included=vat_included,
        items=all_items,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------
def extract_document(content: bytes, filename: str) -> DocumentExtractResult:
    """Extract items and supplier info from a КП/invoice/price-list file."""
    suffix = Path(filename).suffix.lower()
    if suffix == '.pdf':
        if len(content)>MAX_FILE:raise UploadTooLarge(TOO_LARGE)
        if not content.startswith(b'%PDF-'):
            raise ValueError('PDF is invalid')
        return _extract_pdf_isolated(content, filename)
    if suffix == '.xls':
        raise ValueError('legacy .xls is not supported; convert the file to .xlsx and review it before import')
    if suffix == '.xlsx':
        return extract_from_xlsx(content, filename)
    raise ValueError(f'unsupported file type for price-list extraction: {suffix!r}')
