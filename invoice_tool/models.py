from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
import re
from typing import Any

VALID_INVOICE_NUMBER_LENGTHS = frozenset({8, 20})


@dataclass(frozen=True)
class OCRLine:
    text: str
    box: tuple[tuple[float, float], ...]
    score: float | None
    layout_box: tuple[tuple[float, float], ...] | None = None

    @property
    def center(self) -> tuple[float, float]:
        box = self.layout_box or self.box
        return (
            sum(point[0] for point in box) / len(box),
            sum(point[1] for point in box) / len(box),
        )


@dataclass
class InvoiceRecord:
    index: int
    file_path: str
    original_filename: str
    invoice_number: str | None = None
    invoice_code: str | None = None
    invoice_date: str | None = None
    amount: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None
    seller_name: str | None = None
    status: str = "待复核"
    validation_result: list[str] = field(default_factory=list)
    error: str = ""
    invoice_number_issue: str = ""
    raw_text: str = ""
    raw_ocr: list[dict[str, Any]] = field(default_factory=list)
    original_extracted_fields: dict[str, Any] = field(default_factory=dict)
    image_sha256: str = ""
    recognition_source: str = "local"
    source_page: int | None = None
    source_pages: list[int] = field(default_factory=list)

    def as_cloud_fields(self) -> dict[str, Any]:
        return {
            "invoice_number": self.invoice_number,
            "invoice_code": self.invoice_code,
            "invoice_date": self.invoice_date,
            "amount": str(self.amount) if self.amount is not None else None,
            "tax": str(self.tax) if self.tax is not None else None,
            "total": str(self.total) if self.total is not None else None,
            "seller_name": self.seller_name,
        }


def parse_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01")) if value.is_finite() else None
    cleaned = str(value).strip().replace("￥", "").replace("¥", "").replace("元", "")
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    if not re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?", cleaned):
        return None
    try:
        parsed = Decimal(cleaned.replace(",", "")).quantize(Decimal("0.01"))
        return parsed if parsed.is_finite() else None
    except Exception:
        return None


def validate_invoice_number(value: Any) -> tuple[str | None, str]:
    """Accept only common Chinese invoice-number lengths; never pad or truncate."""
    if value is None or str(value).strip() == "":
        return None, ""
    number = re.sub(r"\s+", "", str(value))
    if not number.isdigit():
        return None, "发票号码包含非数字字符，已留空复核"
    if len(number) not in VALID_INVOICE_NUMBER_LENGTHS:
        return None, f"发票号码位数异常（{len(number)} 位），已留空复核"
    return number, ""


def record_from_cloud(index: int, path: str, fields: dict[str, Any]) -> InvoiceRecord:
    file_path = str(Path(path).resolve())
    invoice_number, invoice_number_issue = validate_invoice_number(fields.get("invoice_number"))
    return InvoiceRecord(
        index=index,
        file_path=file_path,
        original_filename=Path(path).name,
        invoice_number=invoice_number,
        invoice_code=str(fields.get("invoice_code") or "").strip() or None,
        invoice_date=str(fields.get("invoice_date") or "").strip() or None,
        amount=parse_decimal(fields.get("amount")),
        tax=parse_decimal(fields.get("tax")),
        total=parse_decimal(fields.get("total")),
        seller_name=str(fields.get("seller_name") or "").strip() or None,
        invoice_number_issue=invoice_number_issue,
        recognition_source="cloud (user-confirmed)",
    )
