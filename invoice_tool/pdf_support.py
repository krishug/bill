from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image

from .models import InvoiceRecord, OCRLine
from .parser import invoice_number_label_count, invoice_numbers_on_page, parse_invoice
from .validation import validate_record

PDF_RENDER_SCALE = 2.0  # 144 dpi from PDF's 72 point/inch coordinates.
REQUIRED_PAGE_FIELDS = ("invoice_number", "amount", "tax", "total", "seller_name")


def extract_page_text_lines(page: pymupdf.Page) -> list[OCRLine]:
    """Extract text spans and their PDF-point coordinates without rendering."""
    lines: list[OCRLine] = []
    try:
        blocks = page.get_text("dict").get("blocks", [])
    except Exception:
        return lines
    for block in blocks:
        if block.get("type") != 0:
            continue
        for text_line in block.get("lines", []):
            for span in text_line.get("spans", []):
                text = str(span.get("text", "")).strip()
                bbox = span.get("bbox")
                if not text or not bbox or len(bbox) != 4:
                    continue
                x0, y0, x1, y1 = (float(value) for value in bbox)
                points = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
                # A PDF text layer has no OCR confidence. Keep score=None rather
                # than inventing a confidence value for digital text.
                lines.append(OCRLine(text=text, box=points, score=None, layout_box=points))
    return lines


def process_pdf_page(
    path: str | Path,
    page: pymupdf.Page,
    page_number: int,
    ocr_backend: Any,
) -> InvoiceRecord:
    source_path = Path(path)
    pdf_lines = extract_page_text_lines(page)
    pdf_size = (max(1, round(page.rect.width)), max(1, round(page.rect.height)))
    pdf_parsed = parse_invoice(pdf_lines, pdf_size)
    pdf_validated = validate_record(pdf_parsed)

    # Text pages that yield all required values and a consistent total avoid
    # rasterization. Incomplete or conflicting text pages fall back to OCR.
    needs_ocr = bool(
        not pdf_lines
        or pdf_validated.status != "成功"
        or len(invoice_numbers_on_page(pdf_lines, pdf_size)) != 1
        or invoice_number_label_count(pdf_lines, pdf_size) != 1
    )
    ocr_lines: list[OCRLine] = []
    ocr_parsed: InvoiceRecord | None = None
    if needs_ocr:
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(PDF_RENDER_SCALE, PDF_RENDER_SCALE), alpha=False)
        page_image = Image.open(BytesIO(pixmap.tobytes("png"))).convert("RGB")
        try:
            ocr_lines, ocr_size = ocr_backend.recognize_image(page_image)
        finally:
            page_image.close()
        if ocr_lines:
            ocr_parsed = validate_record(parse_invoice(ocr_lines, ocr_size))

    chosen = _choose_parse(pdf_parsed, ocr_parsed)
    source = "local PDF text" if chosen is pdf_parsed else "local OCR (PDF page)"
    raw_text_parts = []
    if pdf_lines:
        raw_text_parts.append(f"[PDF第 {page_number} 页文字层]\n" + pdf_parsed.raw_text)
    if ocr_lines:
        ocr_text = ocr_parsed.raw_text if ocr_parsed else ""
        raw_text_parts.append(f"[PDF第 {page_number} 页OCR]\n" + ocr_text)

    record = InvoiceRecord(
        index=0,
        file_path=str(source_path.resolve()),
        original_filename=source_path.name,
        invoice_number=chosen.invoice_number,
        invoice_code=chosen.invoice_code,
        invoice_date=chosen.invoice_date,
        amount=chosen.amount,
        tax=chosen.tax,
        total=chosen.total,
        seller_name=chosen.seller_name,
        invoice_number_issue=chosen.invoice_number_issue,
        raw_text="\n\n".join(raw_text_parts),
        raw_ocr=_page_ocr_rows(chosen.raw_ocr, page_number, source),
        original_extracted_fields=chosen.as_cloud_fields(),
        source_page=page_number,
        source_pages=[page_number],
        recognition_source=source,
    )

    found_numbers = list(dict.fromkeys(
        invoice_numbers_on_page(pdf_lines, pdf_size)
        + (invoice_numbers_on_page(ocr_lines, ocr_size) if ocr_lines else [])
    ))
    number_label_count = max(
        invoice_number_label_count(pdf_lines, pdf_size),
        invoice_number_label_count(ocr_lines, ocr_size) if ocr_lines else 0,
    )
    if len(found_numbers) > 1 or number_label_count > 1:
        record.invoice_number = None
        record.invoice_number_issue = (
            "PDF页包含多个不同发票号码，未自动拆分，待人工确认"
            if len(found_numbers) > 1
            else "PDF页重复出现发票号码标签，可能含多张/重复发票，未自动拆分"
        )
        record.invoice_code = None
        record.invoice_date = None
        record.amount = record.tax = record.total = None
        record.seller_name = None
    elif not found_numbers:
        record.invoice_number = None
        record.invoice_number_issue = "PDF页未找到唯一票面发票号码，页边界待人工确认"

    if not pdf_lines and not ocr_lines:
        record.error = "PDF空白页或未检测到可识别文本"
    validate_record(record)
    if len(found_numbers) > 1 or number_label_count > 1:
        _add_review(record, record.invoice_number_issue)
    elif not found_numbers:
        _add_review(record, record.invoice_number_issue)
    return record


def make_pdf_error_record(path: str | Path, reason: str, page_number: int | None = None) -> InvoiceRecord:
    source_path = Path(path)
    record = InvoiceRecord(
        index=0,
        file_path=str(source_path.resolve()),
        original_filename=source_path.name,
        source_page=page_number,
        source_pages=[page_number] if page_number else [],
        recognition_source="local PDF text",
        error=reason,
    )
    validate_record(record)
    return record


def merge_pdf_continuation_pages(records: list[InvoiceRecord]) -> list[InvoiceRecord]:
    """Merge only clearly identified same-file invoice continuations/copies."""
    output: list[InvoiceRecord] = []
    by_number: dict[tuple[str, str], list[InvoiceRecord]] = {}
    for record in records:
        if Path(record.file_path).suffix.lower() != ".pdf" or not record.invoice_number:
            output.append(record)
            continue
        base_key = (record.file_path, record.invoice_number)
        compatible: list[InvoiceRecord] = []
        for candidate in by_number.get(base_key, []):
            pairs = [
                (candidate.invoice_code, record.invoice_code),
                (candidate.seller_name, record.seller_name),
                (candidate.invoice_date, record.invoice_date),
            ]
            available = [(left, right) for left, right in pairs if left and right]
            if available and all(str(left).casefold() == str(right).casefold() for left, right in available):
                compatible.append(candidate)
        if len(compatible) != 1:
            by_number.setdefault(base_key, []).append(record)
            output.append(record)
            if len(compatible) > 1:
                for candidate in compatible + [record]:
                    _add_review(candidate, "同一 PDF 出现相同发票号码但边界不明确，未自动合并")
            continue
        previous = compatible[0]

        previous.source_pages = sorted(set(previous.source_pages + record.source_pages))
        previous.raw_text = f"{previous.raw_text}\n\n--- 合并 PDF 第 {record.source_page} 页 ---\n\n{record.raw_text}".strip()
        for line in record.raw_ocr:
            previous.raw_ocr.append({**line, "sequence": len(previous.raw_ocr) + 1})
        conflicts: list[str] = []
        for field_name in ("amount", "tax", "total", "seller_name", "invoice_code", "invoice_date"):
            old_value = getattr(previous, field_name)
            new_value = getattr(record, field_name)
            if old_value is None:
                setattr(previous, field_name, new_value)
            elif new_value is not None and old_value != new_value:
                setattr(previous, field_name, None)
                conflicts.append(field_name)
        if conflicts:
            previous.original_extracted_fields["merged_page_conflicts"] = conflicts
            _add_review(previous, "同一 PDF 发票的多页字段存在冲突，相关字段已留空待人工确认")
        validate_record(previous)
        if conflicts:
            _add_review(previous, "同一 PDF 发票的多页字段存在冲突，相关字段已留空待人工确认")
    return output


def _choose_parse(pdf_parsed: InvoiceRecord, ocr_parsed: InvoiceRecord | None) -> InvoiceRecord:
    if ocr_parsed is None:
        return pdf_parsed

    def quality(record: InvoiceRecord) -> tuple[int, int, int]:
        present = sum(getattr(record, name) not in (None, "") for name in REQUIRED_PAGE_FIELDS)
        mismatch = sum("不一致" in issue for issue in record.validation_result)
        ambiguity = sum("多个不同发票号码" in issue or "识别不清" in issue for issue in record.validation_result)
        return present, -mismatch, -ambiguity

    return ocr_parsed if quality(ocr_parsed) > quality(pdf_parsed) else pdf_parsed


def _page_ocr_rows(rows: list[dict[str, Any]], page_number: int, source: str) -> list[dict[str, Any]]:
    coordinate_unit = "pdf-point" if source == "local PDF text" else "pixel"
    return [
        {**row, "page_number": page_number, "source": source, "coordinate_unit": coordinate_unit}
        for row in rows
    ]


def _add_review(record: InvoiceRecord, message: str) -> None:
    if message and message not in record.validation_result:
        record.validation_result.append(message)
    if record.status != "失败":
        record.status = "待复核"
