from __future__ import annotations

from decimal import Decimal

from .models import InvoiceRecord, validate_invoice_number


def validate_record(record: InvoiceRecord) -> InvoiceRecord:
    issues: list[str] = []
    if record.invoice_number:
        record.invoice_number, record.invoice_number_issue = validate_invoice_number(record.invoice_number)
    if not record.invoice_number:
        issues.append(record.invoice_number_issue or "缺少发票号码")
    for field, label in ((record.amount, "金额"), (record.tax, "税额"), (record.total, "价税合计"), (record.seller_name, "销售方公司名称")):
        if field is None or field == "":
            issues.append(f"缺少{label}")

    if record.amount is not None and record.tax is not None and record.total is not None:
        difference = (record.total - record.amount - record.tax).quantize(Decimal("0.01"))
        if difference < 0:
            issues.append(f"价税合计低于金额+税额（少 {abs(difference):.2f} 元）")
        elif difference > Decimal("0.01"):
            issues.append(f"金额+税额与价税合计不一致（多 {difference:.2f} 元）")

    if record.error:
        record.status = "失败"
        record.validation_result = [record.error]
    else:
        record.validation_result = issues or ["校验通过"]
        record.status = "待复核" if issues else "成功"
    return record


def mark_duplicates(records: list[InvoiceRecord]) -> None:
    number_groups: dict[str, list[InvoiceRecord]] = {}
    hash_groups: dict[str, list[InvoiceRecord]] = {}
    for record in records:
        if record.invoice_number:
            number_groups.setdefault(record.invoice_number.strip(), []).append(record)
        if record.image_sha256:
            hash_groups.setdefault(record.image_sha256, []).append(record)

    for group in number_groups.values():
        if len(group) < 2:
            continue
        for index, record in enumerate(group):
            for other in group[index + 1 :]:
                comparisons = [
                    (record.invoice_code, other.invoice_code),
                    (record.seller_name, other.seller_name),
                    (record.invoice_date, other.invoice_date),
                ]
                available = [(left, right) for left, right in comparisons if left and right]
                if any(str(left).casefold() != str(right).casefold() for left, right in available):
                    message = "发票号码相同但发票代码/销售方/日期有差异，需人工核验"
                else:
                    message = "发票号码重复候选（结合可用辅助字段复核；保留记录，不自动删除）"
                for item in (record, other):
                    if message not in item.validation_result:
                        item.validation_result.append(message)
                    if item.status != "失败":
                        item.status = "待复核"
    for group in hash_groups.values():
        if len(group) > 1:
            for record in group:
                if "重复图片" not in record.validation_result:
                    record.validation_result.append("重复图片（保留记录，需人工确认）")
                if record.status != "失败":
                    record.status = "待复核"
