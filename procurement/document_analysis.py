from __future__ import annotations

import io
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from .models import LotItemCreate, ProcurementSuggestionCreate


def _number(value: str) -> Decimal:
    return Decimal(value.replace(" ", "").replace("\xa0", "").replace(",", "."))


def extract_pdf_page(content: bytes, page_number: int) -> str:
    if not content.startswith(b"%PDF-"):
        raise ValueError("invalid PDF payload")
    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception as exc:
        raise ValueError("PDF cannot be read") from exc
    if page_number < 1 or page_number > len(reader.pages):
        raise ValueError(f"PDF page must be between 1 and {len(reader.pages)}")
    text = reader.pages[page_number - 1].extract_text() or ""
    if not text.strip():
        raise ValueError("PDF page has no extractable text; OCR is required")
    return text


def extract_bulat_fence_schedule(
    text: str, *, page_number: int, source_document_id: int | None = None
) -> ProcurementSuggestionCreate:
    flags = re.IGNORECASE | re.DOTALL
    items: list[LotItemCreate] = []
    panel_pattern = re.compile(
        r"П(?P<code>[123])\s*(?:Заводской\s+поставки)?\s*"
        r"Панель\s+ограждения\s+типа\s*[\"«]Булат[\"»],?\s*"
        r"высота\s*(?P<height>\d+[,.]\d+)\s*м\s*"
        r"(?P<quantity>\d+)\s*l\s*=\s*(?P<length>\d+)",
        flags,
    )
    for match in panel_pattern.finditer(text):
        code = f"П{match.group('code')}"
        height_mm = int(_number(match.group("height")) * 1000)
        length_mm = int(match.group("length"))
        items.append(
            LotItemCreate(
                name=f"Панель ограждения «Булат» {code}",
                quantity=int(match.group("quantity")),
                unit="шт.",
                specification=f"высота {height_mm} мм; длина {length_mm} мм",
                source_document_id=source_document_id,
                source_page=page_number,
                source_reference=f"Спецификация элементов, поз. {code}",
            )
        )

    simple_patterns = (
        (
            "Комплект опоры заграждения",
            r"Ст1\s*Комплект\s+опоры\s+заграждения\s*(\d+)\s*шт\.?",
            "шт.",
            "Ст1",
            "",
        ),
        (
            "Калитка",
            r"К1\s*Калитка\s*\(высота\s*([\d,.]+)\s*м,\s*длина\s*([\d,.]+)\s*м\)\s*(\d+)\s*комплект",
            "комплект",
            "К1",
            "dimensions",
        ),
        (
            "Ворота",
            r"В1\s*Ворота\s*\(высота\s*([\d,.]+)\s*м,\s*длина\s*([\d,.]+)\s*м\)\s*(\d+)\s*комплект",
            "комплект",
            "В1",
            "dimensions",
        ),
    )
    for name, pattern, unit, code, mode in simple_patterns:
        match = re.search(pattern, text, flags)
        if not match:
            continue
        if mode == "dimensions":
            height_mm = int(_number(match.group(1)) * 1000)
            length_mm = int(_number(match.group(2)) * 1000)
            quantity = int(match.group(3))
            specification = f"высота {height_mm} мм; длина {length_mm} мм"
        else:
            quantity = int(match.group(1))
            specification = "заводской комплект"
        items.append(
            LotItemCreate(
                name=name,
                quantity=quantity,
                unit=unit,
                specification=specification,
                source_document_id=source_document_id,
                source_page=page_number,
                source_reference=f"Спецификация элементов, поз. {code}",
            )
        )

    required_codes = {"П1", "П2", "П3", "Ст1", "К1", "В1"}
    found_codes = {
        item.source_reference.rsplit(" ", 1)[-1] for item in items if item.source_reference
    }
    missing_codes = sorted(required_codes - found_codes)
    if missing_codes:
        raise ValueError(
            "fence schedule is incomplete; missing positions: " + ", ".join(missing_codes)
        )

    drawing_match = re.search(r"(СТРМ-[A-ZА-Я0-9.\-]+-ГЧ-\d+)", text)
    drawing = drawing_match.group(1) if drawing_match else "проектный лист"
    location = "УЗА-2 камеры приема СОД" if "УЗА-2" in text else "ограждение объекта"
    return ProcurementSuggestionCreate(
        section_code="АС01-ГЧ-011",
        section_name="Архитектурно-строительные решения — ограждение",
        lot_title=f"Ограждение {location}",
        items=items,
        evidence=[
            f"PDF, страница {page_number}",
            drawing,
            "Спецификация элементов",
        ],
        confidence=0.99,
    )


def extract_bulat_invoice(text: str) -> dict[str, Any]:
    panel = re.search(
        r"3D\s+панель\s+[\"«](?P<name>[^\"»]+)[\"»].*?"
        r"В(?P<height>\d+)хШ(?P<width>\d+)мм.*?"
        r"НФ-\d+\s+(?P<quantity>\d+)\s+шт\.\s+"
        r"(?P<unit_price>[\d\s]+,\d{2})\s+(?P<total>[\d\s]+,\d{2})",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    post = re.search(
        r"Столб\s+(?P<section>\d+х\d+х\d+)мм,\s*L=(?P<length>\d+)мм.*?"
        r"НФ-\d+\s+(?P<quantity>\d+)\s+шт\.",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not panel or not post:
        raise ValueError("reference invoice is not a supported Bulat fence invoice")
    invoice_match = re.search(r"Счет\s+на\s+оплату\s+№\s*(\d+)", text, re.IGNORECASE)
    supplier_match = re.search(
        r"Поставщик\s+(.*?),\s+ИНН\s+(\d+)", text, re.IGNORECASE | re.DOTALL
    )
    return {
        "invoice_number": invoice_match.group(1) if invoice_match else "",
        "supplier_name": (
            " ".join(supplier_match.group(1).split()) if supplier_match else ""
        ),
        "supplier_tax_id": supplier_match.group(2) if supplier_match else "",
        "panel": {
            "name": panel.group("name"),
            "height_mm": int(panel.group("height")),
            "width_mm": int(panel.group("width")),
            "quantity": int(panel.group("quantity")),
            "unit_price": str(_number(panel.group("unit_price"))),
            "total": str(_number(panel.group("total"))),
        },
        "support": {
            "section": post.group("section"),
            "length_mm": int(post.group("length")),
            "quantity": int(post.group("quantity")),
        },
        "has_wicket": bool(re.search(r"\bКалитка\b", text, re.IGNORECASE)),
        "has_gate": bool(re.search(r"\bВорота\b", text, re.IGNORECASE)),
        "extra_components": [
            value
            for value, marker in (
                ("заглушка ПВХ", "Заглушка ПВХ"),
                ("скоба монтажная", "Скоба монтажная"),
                ("саморез кровельный", "Саморез кровельный"),
                ("кронштейн Y", "Кронштейн Y"),
            )
            if marker.casefold() in text.casefold()
        ],
    }


def compare_fence_documents(
    project_items: list[dict[str, Any]], reference: dict[str, Any]
) -> dict[str, Any]:
    panels = [item for item in project_items if item["name"].startswith("Панель ограждения")]
    supports = [item for item in project_items if item["name"] == "Комплект опоры заграждения"]
    wickets = [item for item in project_items if item["name"] == "Калитка"]
    gates = [item for item in project_items if item["name"] == "Ворота"]
    if not panels or not supports:
        raise ValueError("project suggestion is not a supported fence schedule")

    project_panel_quantity = sum(int(Decimal(str(item["quantity"]))) for item in panels)
    project_heights = sorted(
        {
            int(match.group(1))
            for item in panels
            if (match := re.search(r"высота\s+(\d+)\s+мм", item["specification"]))
        }
    )
    project_lengths = sorted(
        {
            int(match.group(1))
            for item in panels
            if (match := re.search(r"длина\s+(\d+)\s+мм", item["specification"]))
        }
    )
    discrepancies: list[dict[str, Any]] = []

    def add(code: str, expected: Any, actual: Any, message: str) -> None:
        if expected != actual:
            discrepancies.append(
                {"code": code, "project": expected, "reference": actual, "message": message}
            )

    add(
        "panel_quantity",
        project_panel_quantity,
        reference["panel"]["quantity"],
        "Количество панелей в счёте не соответствует проекту",
    )
    add(
        "panel_height_mm",
        project_heights,
        [reference["panel"]["height_mm"]],
        "Высота панелей в счёте не соответствует проекту",
    )
    add(
        "panel_lengths_mm",
        project_lengths,
        [reference["panel"]["width_mm"]],
        "Набор длин панелей в счёте не соответствует проекту",
    )
    add(
        "support_quantity",
        int(Decimal(str(supports[0]["quantity"]))),
        reference["support"]["quantity"],
        "Количество опор в счёте не соответствует проекту",
    )
    add(
        "wicket_presence",
        bool(wickets),
        reference["has_wicket"],
        "В проекте предусмотрена калитка, а в счёте её нет",
    )
    add(
        "gate_presence",
        bool(gates),
        reference["has_gate"],
        "В проекте предусмотрены ворота, а в счёте их нет",
    )

    return {
        "status": "compatible" if not discrepancies else "mismatch",
        "can_use_as_current_quote": not discrepancies,
        "allowed_use": ["supplier_registry", "price_history"] if discrepancies else ["quote"],
        "discrepancies": discrepancies,
        "reference": reference,
        "policy": "project_schedule_is_authoritative",
    }


def read_stored_pdf(path: str) -> bytes:
    file_path = Path(path)
    if file_path.suffix.casefold() != ".pdf" or not file_path.is_file():
        raise ValueError("stored source document is not an available PDF")
    return file_path.read_bytes()
