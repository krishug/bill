from __future__ import annotations

import re
from collections.abc import Iterable
from decimal import Decimal

from .models import InvoiceRecord, OCRLine, parse_decimal

MONEY_RE = re.compile(
    r"(?<![\d])(?:[¥￥]\s*)?([+-]?\s*(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d{1,2})?)(?!\d)"
)
INVOICE_NO_RE = re.compile(r"(?<!\d)(\d{8,30})(?!\d)")
INVOICE_CODE_RE = re.compile(r"(?<!\d)(\d{8,20})(?!\d)")
DATE_RE = re.compile(r"(?<!\d)(20\d{2})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})\s*日?(?!\d)")
_SPACE_RE = re.compile(r"\s+")
MIN_INVOICE_NUMBER_SCORE = 0.85


def _clean(text: str) -> str:
    return _SPACE_RE.sub("", text).replace("：", ":").replace("﹕", ":")


def _money_values(text: str) -> list[Decimal]:
    found: list[Decimal] = []
    for match in MONEY_RE.finditer(text.replace("，", ",")):
        value = parse_decimal(match.group(1).replace(" ", ""))
        if value is not None:
            found.append(value)
    return found


def _rows(lines: list[OCRLine], image_height: float) -> list[list[OCRLine]]:
    """Group nearby OCR boxes into visual rows without discarding coordinates."""
    if not lines:
        return []
    ordered = sorted(lines, key=lambda item: (item.center[1], item.center[0]))
    threshold = max(8.0, image_height * 0.012)
    rows: list[list[OCRLine]] = []
    for line in ordered:
        y = line.center[1]
        target = next(
            (row for row in reversed(rows) if abs(y - sum(x.center[1] for x in row) / len(row)) <= threshold),
            None,
        )
        if target is None:
            rows.append([line])
        else:
            target.append(line)
    return [sorted(row, key=lambda item: item.center[0]) for row in rows]


def _row_text(row: Iterable[OCRLine]) -> str:
    return " ".join(line.text for line in row)


def _number_candidates(text: str, pattern: re.Pattern[str], label: str) -> list[str]:
    compact = _clean(text)
    if label not in compact:
        return []
    tail = compact.split(label, 1)[1]
    # Do not consume unrelated identifiers if several invoice fields share one OCR box.
    tail = re.split(r"发票代码|发票号码|开票日期|校验码|机器编号|纳税人识别号|统一社会信用代码|购买方|销售方", tail)[0]
    return [match.group(1) for match in pattern.finditer(tail)]


def _labelled_invoice_numbers(lines: list[OCRLine], rows: list[list[OCRLine]]) -> tuple[list[str], bool]:
    values: list[str] = []
    for row in rows:
        row_compact = _clean(_row_text(row))
        if "发票号码" not in row_compact:
            continue
        label_found = True
        for line in row:
            compact = _clean(line.text)
            if "发票号码" not in compact:
                continue
            if line.score is not None and line.score < MIN_INVOICE_NUMBER_SCORE:
                continue
            candidates = _number_candidates(line.text, INVOICE_NO_RE, "发票号码")
            if not candidates:
                anchor_x = line.center[0]
                for candidate in row:
                    if candidate is line or any(
                        token in _clean(candidate.text)
                        for token in ("税号", "纳税", "识别号", "统一社会信用代码", "发票代码", "校验", "金额", "日期")
                    ):
                        continue
                    if candidate.score is not None and candidate.score < MIN_INVOICE_NUMBER_SCORE:
                        continue
                    if candidate.center[0] < anchor_x:
                        continue
                    candidates.extend(match.group(1) for match in INVOICE_NO_RE.finditer(_clean(candidate.text)))
            values.extend(candidates)
        if not any("发票号码" in _clean(line.text) for line in row):
            # Some PDF text extractors split 发票 and 号码 into adjacent spans.
            fragments = [line for line in row if any(token in _clean(line.text) for token in ("发票", "号码"))]
            if any(
                fragment.score is not None and fragment.score < MIN_INVOICE_NUMBER_SCORE
                for fragment in fragments
            ):
                continue
            anchor_x = max((line.center[0] for line in fragments), default=-1.0)
            for candidate in row:
                text = _clean(candidate.text)
                if candidate.center[0] <= anchor_x or any(
                    token in text
                    for token in ("税号", "纳税", "识别号", "统一社会信用代码", "发票代码", "校验", "金额", "日期")
                ):
                    continue
                if candidate.score is not None and candidate.score < MIN_INVOICE_NUMBER_SCORE:
                    continue
                values.extend(match.group(1) for match in INVOICE_NO_RE.finditer(text))
    label_found = any("发票号码" in _clean(_row_text(row)) for row in rows)
    return list(dict.fromkeys(values)), label_found


def invoice_numbers_on_page(lines: list[OCRLine], image_size: tuple[int, int]) -> list[str]:
    """Return unique numbers explicitly attached to 发票号码 labels on a page."""
    rows = _rows(lines, float(image_size[1]))
    return _labelled_invoice_numbers(lines, rows)[0]


def invoice_number_label_count(lines: list[OCRLine], image_size: tuple[int, int]) -> int:
    """Count printed invoice-number labels, even repeated copies of one number."""
    return sum(_clean(_row_text(row)).count("发票号码") for row in _rows(lines, float(image_size[1])))


def _labelled_value(lines: list[OCRLine], rows: list[list[OCRLine]], label: str, pattern: re.Pattern[str]) -> str | None:
    for row in rows:
        for line in row:
            values = _number_candidates(line.text, pattern, label)
            if values:
                return values[0]
    return None


def _invoice_date(lines: list[OCRLine], rows: list[list[OCRLine]]) -> str | None:
    for row in rows:
        compact = _clean(_row_text(row))
        if "开票日期" not in compact and "开票时间" not in compact:
            continue
        tail = re.split(r"开票日期|开票时间", compact, maxsplit=1)[-1]
        match = DATE_RE.search(tail)
        if match:
            year, month, day = (int(value) for value in match.groups())
            if 1 <= month <= 12 and 1 <= day <= 31:
                return f"{year:04d}-{month:02d}-{day:02d}"
        digits = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", tail)
        if digits:
            year, month, day = (int(value) for value in digits.groups())
            if 1 <= month <= 12 and 1 <= day <= 31:
                return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _value_after_label(lines: list[OCRLine], rows: list[list[OCRLine]], label: str) -> Decimal | None:
    """Extract only a value visually associated with the requested label."""
    compact_label = _clean(label)
    for row in rows:
        text = _row_text(row)
        compact = _clean(text)
        if compact_label not in compact:
            continue
        if label == "价税合计" and "大写" in compact and "小写" not in compact:
            continue
        values = _money_values(text)
        if values:
            # Values following the label are preferred; if OCR split the row,
            # left-to-right order is the natural invoice reading order.
            tail = compact.split(compact_label, 1)[-1]
            tail_values = _money_values(tail)
            return (tail_values or values)[0]

        label_boxes = [line for line in row if compact_label in _clean(line.text)]
        if not label_boxes:
            continue
        label_x, label_y = label_boxes[0].center
        candidates: list[tuple[float, Decimal]] = []
        for candidate in row:
            if candidate in label_boxes:
                continue
            for value in _money_values(candidate.text):
                x, y = candidate.center
                # A number far from the label in another table column is not
                # accepted as a direct label/value pair.
                candidates.append((abs(y - label_y) + abs(x - label_x) * 0.18, value))
        if candidates:
            return min(candidates, key=lambda item: item[0])[1]
    return None


def _summary_values(rows: list[list[OCRLine]]) -> tuple[Decimal | None, Decimal | None]:
    for row in rows:
        text = _clean(_row_text(row))
        if "合计" not in text or "价税合计" in text:
            continue
        values = _money_values(_row_text(row))
        if len(values) >= 2:
            return values[0], values[1]
        if len(values) == 1:
            return values[0], None
    return None, None


def _summary_values_by_columns(
    lines: list[OCRLine], rows: list[list[OCRLine]], image_height: float
) -> tuple[Decimal | None, Decimal | None]:
    """Associate nearby summary-row amounts with the printed 金额/税额 columns."""
    anchors: list[tuple[float, str]] = []
    for row in rows:
        text = _clean(_row_text(row))
        if "合计" in text and "价税合计" not in text and "大写" not in text:
            anchors.append((sum(item.center[1] for item in row) / len(row), text))
    if not anchors:
        return None, None

    # Prefer the last non-tax-inclusive 合计 row: it is the invoice summary,
    # while earlier subtotal rows can represent individual sections.
    summary_y, _ = anchors[-1]
    header_x: dict[str, list[tuple[float, float]]] = {"amount": [], "tax": []}
    total_x: list[float] = []
    for line in lines:
        text = _clean(line.text)
        y, x = line.center[1], line.center[0]
        if "价税合计" in text and "大写" not in text:
            total_x.append(x)
        if _money_values(line.text):
            continue
        if "金额" in text and "价税合计" not in text:
            header_x["amount"].append((abs(summary_y - y), x))
        if "税额" in text and "价税合计" not in text:
            header_x["tax"].append((abs(summary_y - y), x))

    selected_x: dict[str, float] = {}
    for field, candidates in header_x.items():
        if candidates:
            selected_x[field] = min(candidates, key=lambda item: item[0])[1]

    band = max(30.0, image_height * 0.035)
    values: list[tuple[float, float, Decimal]] = []
    for line in lines:
        x, y = line.center
        if abs(y - summary_y) > band:
            continue
        text = _clean(line.text)
        if any(label in text for label in ("价税合计", "大写", "小写")):
            continue
        for value in _money_values(line.text):
            values.append((x, y, value))

    def choose(field: str, other: Decimal | None = None) -> Decimal | None:
        target_x = selected_x.get(field)
        if target_x is None:
            return None
        options: list[tuple[float, Decimal]] = []
        for x, y, value in values:
            if other is not None and value == other:
                continue
            # The total is a separate printed column; never mistake its value
            # for an amount/tax value just because it lies in the summary band.
            if total_x and min(abs(x - position) for position in total_x) < abs(x - target_x):
                continue
            options.append((abs(x - target_x) + abs(y - summary_y) * 0.35, value))
        return min(options, key=lambda item: item[0])[1] if options else None

    amount = choose("amount")
    tax = choose("tax", amount)
    return amount, tax


def _seller(lines: list[OCRLine], rows: list[list[OCRLine]]) -> str | None:
    # Invoice layouts commonly print buyer and seller blocks separately. Prefer
    # the block explicitly introduced by 销售方, then its 名称 field.
    for index, row in enumerate(rows):
        anchor = next((line for line in row if "销售方" in _clean(line.text)), None)
        if not anchor:
            continue
        ax, ay = anchor.center
        nearby: list[tuple[float, str]] = []
        for candidate in (item for nearby_row in rows[index : min(index + 5, len(rows))] for item in nearby_row):
            cleaned = _clean(candidate.text)
            if "名称" not in cleaned or "销售方" in cleaned or any(
                marker in cleaned for marker in ("纳税人识别号", "统一社会信用代码")
            ):
                continue
            cx, cy = candidate.center
            nearby.append((abs(cy - ay) + abs(cx - ax) * 0.1, candidate.text))
        if nearby:
            text = min(nearby, key=lambda item: item[0])[1]
            match = re.search(r"名称\s*[:：]?\s*(.+)", text)
            if match:
                name = match.group(1).strip()
                for marker in ("纳税人识别号", "统一社会信用代码"):
                    name = name.split(marker, 1)[0].strip(" ：:")
                if name:
                    return name

    # Fallback for classic invoices without a readable seller-side caption:
    # in the standard buyer-then-seller layout, the second 名称 field is seller.
    names: list[str] = []
    for row in rows:
        for line in row:
            text = line.text
            if "名称" not in _clean(text) or any(tag in _clean(text) for tag in ("纳税人识别号", "统一社会信用代码")):
                continue
            match = re.search(r"名称\s*[:：]?\s*(.+)", text)
            if match:
                value = match.group(1).strip()
                if value and value not in names:
                    names.append(value)
    return names[1] if len(names) >= 2 else None


def parse_invoice(lines: list[OCRLine], image_size: tuple[int, int]) -> InvoiceRecord:
    width, height = image_size
    rows = _rows(lines, float(height))
    raw_text = "\n".join(_row_text(row) for row in rows)

    total = _value_after_label(lines, rows, "价税合计")
    summary_amount, summary_tax = _summary_values(rows)
    column_amount, column_tax = _summary_values_by_columns(lines, rows, float(height))
    # The visual 合计 row is more authoritative than an earlier line-item
    # labelled 项目金额. Generic amount/tax labels are only a fallback.
    amount = summary_amount if summary_amount is not None else column_amount
    tax = summary_tax if summary_tax is not None else column_tax
    if amount is None:
        amount = _value_after_label(lines, rows, "金额")
    if tax is None:
        tax = _value_after_label(lines, rows, "税额")
    if tax is None and any(token in raw_text for token in ("不征税", "免税")):
        # Explicit exemption wording is an observed zero-tax condition; a blank
        # tax field by itself is never converted to zero.
        tax = Decimal("0.00")

    invoice_number_candidates, invoice_number_label_found = _labelled_invoice_numbers(lines, rows)
    invoice_number = invoice_number_candidates[0] if len(invoice_number_candidates) == 1 else None
    if len(invoice_number_candidates) > 1:
        invoice_number_issue = "票面出现多个不同发票号码，边界不明确，已留空"
    elif not invoice_number_candidates and invoice_number_label_found:
        invoice_number_issue = "发票号码识别不清，已留空复核"
    elif not invoice_number_candidates:
        invoice_number_issue = "缺少发票号码"
    else:
        invoice_number_issue = ""

    return InvoiceRecord(
        index=0,
        file_path="",
        original_filename="",
        invoice_number=invoice_number,
        invoice_code=_labelled_value(lines, rows, "发票代码", INVOICE_CODE_RE),
        invoice_date=_invoice_date(lines, rows),
        amount=amount,
        tax=tax,
        total=total,
        seller_name=_seller(lines, rows),
        invoice_number_issue=invoice_number_issue,
        raw_text=raw_text,
        raw_ocr=[
            {
                "text": line.text,
                "box": [list(point) for point in line.box],
                "layout_box": [list(point) for point in line.layout_box] if line.layout_box else None,
                "score": line.score,
            }
            for line in lines
        ],
    )
