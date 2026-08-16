from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProjectCreate(StrictModel):
    name: str = Field(min_length=2, max_length=200)
    region: str = Field(min_length=2, max_length=120)
    delivery_address: str = Field(min_length=3, max_length=500)
    description: str = Field(default="", max_length=4000)


class SectionCreate(StrictModel):
    code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=2, max_length=200)
    description: str = Field(default="", max_length=8000)


class SupplierCreate(StrictModel):
    name: str = Field(min_length=2, max_length=240)
    tax_id: str = Field(default="", max_length=32)
    region: str = Field(min_length=2, max_length=120)
    email: str = Field(default="", max_length=320)
    phone: str = Field(default="", max_length=40)
    telegram: str = Field(default="", max_length=120)
    categories: list[str] = Field(default_factory=list, max_length=50)
    rating: float = Field(default=3.0, ge=0, le=5)
    verified: bool = False

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if value and ("@" not in value or value.startswith("@") or value.endswith("@")):
            raise ValueError("invalid email")
        return value.lower()


class LotItemCreate(StrictModel):
    name: str = Field(min_length=2, max_length=240)
    quantity: Decimal = Field(gt=0)
    unit: str = Field(min_length=1, max_length=30)
    specification: str = Field(default="", max_length=8000)


class LotCreate(StrictModel):
    project_id: int = Field(gt=0)
    section_id: int | None = Field(default=None, gt=0)
    title: str = Field(min_length=2, max_length=240)
    region: str = Field(min_length=2, max_length=120)
    delivery_address: str = Field(min_length=3, max_length=500)
    response_deadline: date
    desired_delivery_date: date | None = None
    currency: str = Field(default="RUB", pattern=r"^[A-Z]{3}$")
    items: list[LotItemCreate] = Field(min_length=1, max_length=500)


class ProcurementSuggestionCreate(StrictModel):
    section_code: str = Field(min_length=1, max_length=50)
    section_name: str = Field(min_length=2, max_length=200)
    lot_title: str = Field(min_length=2, max_length=240)
    items: list[LotItemCreate] = Field(min_length=1, max_length=500)
    evidence: list[str] = Field(default_factory=list, max_length=50)
    confidence: float = Field(ge=0, le=1)


class ProcurementSuggestionBatch(StrictModel):
    suggestions: list[ProcurementSuggestionCreate] = Field(min_length=1, max_length=100)


class ProcurementSuggestionApproval(StrictModel):
    response_deadline: date
    desired_delivery_date: date | None = None
    currency: str = Field(default="RUB", pattern=r"^[A-Z]{3}$")
    approved_by: str = Field(min_length=2, max_length=160)


class ProcurementSuggestionRejection(StrictModel):
    reviewed_by: str = Field(min_length=2, max_length=160)
    reason: str = Field(min_length=2, max_length=1000)


class CampaignCreate(StrictModel):
    template_code: str = Field(default="rfq-email", min_length=2, max_length=80)
    supplier_ids: list[int] = Field(min_length=1, max_length=500)
    channel: Literal["email", "telegram", "whatsapp"] = "email"


class QuoteItemCreate(StrictModel):
    lot_item_id: int = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    offered_quantity: Decimal | None = Field(default=None, gt=0)
    compliant: bool = True
    note: str = Field(default="", max_length=2000)


class QuoteCreate(StrictModel):
    supplier_id: int = Field(gt=0)
    currency: str = Field(default="RUB", pattern=r"^[A-Z]{3}$")
    vat_included: bool = True
    delivery_cost: Decimal = Field(default=Decimal("0"), ge=0)
    lead_days: int = Field(default=0, ge=0, le=3650)
    payment_terms: str = Field(default="", max_length=1000)
    warranty: str = Field(default="", max_length=1000)
    valid_until: date | None = None
    source_filename: str = Field(default="", max_length=255)
    items: list[QuoteItemCreate] = Field(min_length=1, max_length=500)


class PurchaseHistoryCreate(StrictModel):
    supplier_id: int | None = Field(default=None, gt=0)
    source_document_id: int | None = Field(default=None, gt=0)
    item_name: str = Field(min_length=2, max_length=240)
    quantity: Decimal = Field(gt=0)
    unit: str = Field(min_length=1, max_length=30)
    unit_price: Decimal = Field(gt=0)
    currency: str = Field(default="RUB", pattern=r"^[A-Z]{3}$")
    vat_included: bool = True
    purchased_on: date
    invoice_number: str = Field(default="", max_length=120)
    project_name: str = Field(default="", max_length=240)
    region: str = Field(default="", max_length=120)
    confirmed_by: str = Field(min_length=2, max_length=160)


class TemplateUpsert(StrictModel):
    name: str = Field(min_length=2, max_length=160)
    subject: str = Field(min_length=2, max_length=500)
    body: str = Field(min_length=10, max_length=30000)


class ApprovalDecision(StrictModel):
    approved_by: str = Field(min_length=2, max_length=160)
    comment: str = Field(default="", max_length=1000)
