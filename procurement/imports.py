from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from openpyxl import load_workbook

from .models import SupplierCreate


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
        try:
            supplier = _supplier_from_row(list(row), mapping)
            if supplier is not None:
                preview.rows.append(supplier)
        except Exception as exc:
            preview.errors.append({"row": row_number, "error": str(exc)})
    return preview


def parse_supplier_table(content: bytes, filename: str) -> ImportPreview:
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        text = content.decode("utf-8-sig")
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
            dialect.delimiter = ";"
        return _parse_rows(tuple(row) for row in csv.reader(io.StringIO(text), dialect))
    if suffix == ".xlsx":
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            sheet = workbook.active
            return _parse_rows(sheet.iter_rows(values_only=True))
        finally:
            workbook.close()
    raise ValueError("only .csv and .xlsx supplier tables are supported")
