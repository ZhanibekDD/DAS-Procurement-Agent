"""Bounded, deterministic spreadsheet parsing for human-reviewed procurement."""
import csv
import io
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openpyxl import load_workbook

from .upload_io import MAX_FILE, TOO_LARGE, UploadTooLarge, open_payload
MAX_ROWS = 10000
MAX_COLS = 100


def validate_xlsx_expansion(content: bytes, filename: str) -> None:
    """Check actual worksheet XML, not attacker-controlled dimension hints."""
    from xml.etree import ElementTree as ET
    safe_upload(content, filename, {'.xlsx'})
    with open_payload(content) as source, zipfile.ZipFile(source) as archive:
        if sum(i.file_size for i in archive.infolist()) > 20 * 1024 * 1024:
            raise ValueError('XLSX expanded data exceeds 20 MB')
        sheets = [i for i in archive.namelist() if re.fullmatch(r'xl/worksheets/[^/]+\.xml', i)]
        if len(sheets) > 20:
            raise ValueError('XLSX exceeds 20 sheets')
        rows = cells = strings = 0
        parts = sheets + (['xl/sharedStrings.xml'] if 'xl/sharedStrings.xml' in archive.namelist() else [])
        for part in parts:
            with archive.open(part) as stream:
                for _, node in ET.iterparse(stream, events=('end',)):
                    tag = node.tag.rsplit('}', 1)[-1]
                    if tag == 'c':
                        cells += 1
                        match = re.fullmatch(r'([A-Z]+)(\d+)', node.attrib.get('r', ''))
                        if not match:
                            raise ValueError('XLSX invalid cell coordinates')
                        column = 0
                        for char in match[1]:
                            column = column * 26 + ord(char) - 64
                        if column > MAX_COLS or int(match[2]) > MAX_ROWS + 100 or cells > 200000:
                            raise ValueError('XLSX row/column/cell limit exceeded')
                    elif tag == 'row':
                        rows += 1
                        if rows > MAX_ROWS + 100 or int(node.attrib.get('r', '0')) > MAX_ROWS + 100:
                            raise ValueError('XLSX row limit exceeded')
                    elif tag == 't' and len(node.text or '') > 8000:
                        raise ValueError('XLSX cell exceeds 8000 characters')
                    elif tag == 'si':
                        strings += 1
                        if strings > 200000:
                            raise ValueError('XLSX shared-string limit exceeded')
                    node.clear()


def safe_upload(content: bytes, filename: str, allowed: set[str]) -> str:
    if len(content) > MAX_FILE:
        raise UploadTooLarge(TOO_LARGE)
    if not len(content):raise ValueError('Файл пуст')
    if (not filename or len(filename) > 200 or any(c in filename for c in '/\\\r\n\x00:<>"|?*')
            or filename.startswith('.') or Path(filename).suffix.lower() not in allowed):
        raise ValueError('Недопустимое имя или расширение файла')
    if any(s.lower() in {'.exe','.js','.html','.bat','.cmd','.ps1','.vbs','.scr','.com'} for s in Path(filename).suffixes):
        raise ValueError('Опасное двойное расширение')
    suffix = Path(filename).suffix.lower()
    if suffix in {'.xlsx', '.docx'}:
        try:
            with open_payload(content) as source, zipfile.ZipFile(source) as archive:
                infos = archive.infolist()
                if (len(infos) > 20000 or sum(i.file_size for i in infos) > 100 * 1024 * 1024
                        or any('vbaproject' in i.filename.lower() or i.flag_bits & 1 for i in infos)
                        or ('xl/workbook.xml' if suffix == '.xlsx' else 'word/document.xml') not in archive.namelist()):
                    raise ValueError('Недопустимое содержимое Office-файла')
        except zipfile.BadZipFile as exc:
            raise ValueError('Повреждённый Office-файл') from exc
    if suffix == '.pdf' and not content.startswith(b'%PDF-'):
        raise ValueError('Содержимое не соответствует PDF')
    return suffix


def read_table(content: bytes, filename: str, sheet: str = '', header_row: int = 1) -> dict:
    with open_payload(content) as stream:
        return _read_table(content,stream,filename,sheet,header_row)


def _read_table(content,stream,filename,sheet,header_row):
    suffix = safe_upload(content, filename, {'.xlsx', '.csv'})
    if not 1 <= header_row <= 100:
        raise ValueError('Строка заголовков должна быть от 1 до 100')
    if suffix == '.csv':
        # Detect encoding incrementally, never decode the whole uploaded file.
        import codecs
        decoder=codecs.getincrementaldecoder('utf-8-sig')()
        encoding='utf-8-sig'
        try:
            while part:=stream.read(1024*1024):decoder.decode(part)
            decoder.decode(b'',final=True)
        except UnicodeDecodeError:encoding='cp1251'
        stream.seek(0)
        text=io.TextIOWrapper(stream,encoding=encoding,newline='')
        try:
            dialect = csv.Sniffer().sniff(text.read(4096), delimiters=',;\t')
        except csv.Error:
            dialect = csv.excel
        text.seek(0)
        raw = []
        for row in csv.reader(text, dialect):
            if len(raw) >= MAX_ROWS + 100 or len(row) > MAX_COLS:
                raise ValueError('Таблица превышает 10000 строк или 100 колонок')
            raw.append(row)
        sheets = ['CSV']
        selected = 'CSV'
    else:
        validate_xlsx_expansion(content, filename)
        book = load_workbook(stream, read_only=True, data_only=False, keep_links=False)
        try:
            sheets = book.sheetnames
            if sheet and sheet not in sheets:
                raise ValueError('Лист не найден')
            selected = sheet or sheets[0]
            ws = book[selected]
            if (ws.max_row or 0) > MAX_ROWS + 100 or (ws.max_column or 0) > MAX_COLS:
                raise ValueError('Таблица превышает 10000 строк или 100 колонок')
            raw = []
            for row in ws.iter_rows():
                if len(raw) >= MAX_ROWS + 100 or len(row) > MAX_COLS:
                    raise ValueError('Таблица превышает 10000 строк или 100 колонок')
                raw.append([{'formula': True} if c.data_type == 'f' else c.value for c in row])
        finally:
            book.close()
    if len(raw) < header_row or len(raw) > MAX_ROWS + 100 or any(len(r) > MAX_COLS for r in raw):
        raise ValueError('Таблица пуста или слишком велика')
    def cell(v):
        if isinstance(v, dict):
            return v
        if isinstance(v, (datetime, date)):
            return v.date().isoformat() if isinstance(v, datetime) else v.isoformat()
        if v is None:
            return ''
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        s = str(v)
        if len(s) > 8000:
            raise ValueError('Ячейка превышает 8000 символов')
        return s.strip()
    header = [cell(v) for v in raw[header_row - 1]]
    if any(isinstance(v, dict) for v in header):
        raise ValueError('Формулы в заголовке не поддерживаются')
    rows = [{'row': i + header_row + 1, 'cells': [cell(v) for v in r]}
            for i, r in enumerate(raw[header_row:]) if any(v not in ('', None) for v in r)]
    if not rows or len(rows) > MAX_ROWS:
        raise ValueError('Нет строк данных или превышен предел 10000 строк')
    return {'headers': header, 'sheets': sheets, 'sheet': selected, 'header_row': header_row, 'rows': rows}


def suggested_mapping(headers: list[str], aliases: dict[str, set[str]]) -> dict:
    result = {}
    for field, words in aliases.items():
        hits = [i for i, h in enumerate(headers) if ' '.join(h.casefold().split()) in words]
        if len(hits) == 1:
            result[field] = hits[0]
    return result


def mapped(row: dict, mapping: dict, headers: list[str], fields: set[str]) -> dict:
    if (set(mapping) - fields or any(type(v) is not int or not 0 <= v < len(headers) for v in mapping.values())
            or len(set(mapping.values())) != len(mapping)):
        raise ValueError('Некорректное сопоставление колонок')
    values = {k: row['cells'][v] if v < len(row['cells']) else '' for k, v in mapping.items()}
    if any(isinstance(v, dict) for v in values.values()):
        raise ValueError('Формула без проверенного значения: замените её значением')
    return values


def valid_inn(value: str) -> str:
    v = value.strip()
    if not v:
        return ''
    if not re.fullmatch(r'\d{10}|\d{12}', v):
        raise ValueError('ИНН должен содержать 10 или 12 цифр')
    def check(weights, digit):
        return sum(int(n) * w for n, w in zip(v, weights)) % 11 % 10 == int(v[digit])
    if (len(v) == 10 and not check([2,4,10,3,5,9,4,6,8], 9)
            or len(v) == 12 and not (check([7,2,4,10,3,5,9,4,6,8], 10)
                                    and check([3,7,2,4,10,3,5,9,4,6,8], 11))):
        raise ValueError('Неверная контрольная сумма ИНН')
    if len(set(v)) == 1:
        raise ValueError('Недопустимый ИНН')
    return v


def contacts(values: dict) -> dict:
    v = dict(values)
    v['tax_id'] = valid_inn(str(v.get('tax_id', '')))
    email = str(v.get('email', '')).strip().lower()
    if email and not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", email):
        raise ValueError('Некорректная почта')
    phone = str(v.get('phone', '')).strip()
    if phone:
        if not re.fullmatch(r'\+?[0-9 ()-]+', phone):
            raise ValueError('Некорректный телефон')
        digits = re.sub(r'\D', '', phone)
        if not 10 <= len(digits) <= 15:
            raise ValueError('Телефон должен содержать 10–15 цифр')
        phone = ('+' if phone.startswith('+') else '') + digits
    v.update(email=email, phone=phone)
    return v


def quantity(value: str) -> str:
    if not re.fullmatch(r'\d+(?:[.,]\d+)?', value.replace(' ', '')):
        raise ValueError('Количество должно быть положительным числом')
    try:
        n = Decimal(value.replace(' ', '').replace(',', '.'))
    except InvalidOperation as exc:
        raise ValueError('Некорректное количество') from exc
    if not n.is_finite() or n <= 0:
        raise ValueError('Количество должно быть больше нуля')
    return format(n, 'f')


def delivery_date(value: str) -> str | None:
    if not value:
        return None
    for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%d/%m/%Y'):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError('Срок должен быть датой YYYY-MM-DD или ДД.ММ.ГГГГ')
