from decimal import Decimal

from invoice_tool.models import OCRLine
from invoice_tool.parser import parse_invoice
from invoice_tool.validation import mark_duplicates, validate_record


def line(text: str, x: float, y: float, width: float = 80, height: float = 12) -> OCRLine:
    # Slightly skewed quadrilaterals mimic the geometry retained from phone photos.
    return OCRLine(text, ((x, y), (x + width, y + 2), (x + width - 2, y + height), (x - 2, y + height - 2)), 0.91)


def ordinary_lines() -> list[OCRLine]:
    return [
        line("购买方信息", 0, 20),
        line("名称：甲方示例有限公司", 0, 42, 190),
        line("销售方信息", 420, 20),
        line("名称：乙方示例科技有限公司", 420, 42, 220),
        line("发票号码：123456789012", 0, 82, 220),
        line("项目金额 9999.99", 0, 150, 180),
        line("合计", 0, 220, 50),
        line("100.00", 240, 220, 70),
        line("6.00", 400, 220, 50),
        line("价税合计（小写）", 0, 270, 150),
        line("¥106.00", 230, 270, 80),
    ]


def test_extracts_labelled_totals_and_seller_not_unrelated_amounts() -> None:
    record = parse_invoice(ordinary_lines(), (1000, 1000))
    assert record.invoice_number == "123456789012"
    assert record.amount == Decimal("100.00")
    assert record.tax == Decimal("6.00")
    assert record.total == Decimal("106.00")
    assert record.seller_name == "乙方示例科技有限公司"
    assert validate_record(record).status == "成功"


def test_invoice_number_comes_only_from_its_label_and_preserves_leading_zeroes() -> None:
    lines = [
        line("统一社会信用代码/纳税人识别号：91310118759864955D", 0, 10, 360),
        line("发票代码：110012345678", 0, 40, 200),
        line("发票号码：000123456789", 0, 70, 200),
        line("开票日期：2026年10月12日", 0, 100, 220),
    ]
    record = parse_invoice(lines, (1000, 1000))
    assert record.invoice_number == "000123456789"
    assert record.invoice_code == "110012345678"
    assert record.invoice_date == "2026-10-12"


def test_unlabelled_numbers_and_ambiguous_invoice_numbers_are_not_guessed() -> None:
    unlabelled = validate_record(parse_invoice([line("文件编号 123456789012", 0, 20)], (800, 600)))
    assert unlabelled.invoice_number is None
    assert "缺少发票号码" in unlabelled.validation_result

    ambiguous = validate_record(parse_invoice([
        line("发票号码：123456789012", 0, 20, 180),
        line("发票号码：123456789013", 0, 45, 180),
    ], (800, 600)))
    assert ambiguous.invoice_number is None
    assert any("多个不同发票号码" in issue for issue in ambiguous.validation_result)

    taxpayer_only = validate_record(parse_invoice([
        line("发票", 0, 20, 45), line("号码", 55, 20, 45),
        line("纳税人识别号：91310118759864955D", 120, 20, 270),
    ], (800, 600)))
    assert taxpayer_only.invoice_number is None

    unclear = validate_record(parse_invoice([
        OCRLine("发票号码：000123456789", ((0, 10), (200, 10), (200, 25), (0, 25)), 0.40),
    ], (800, 600)))
    assert unclear.invoice_number is None
    assert any("识别不清" in issue for issue in unclear.validation_result)


def test_missing_tax_is_not_silently_converted_to_zero() -> None:
    lines = [
        line("销售方信息", 400, 20), line("名称：测试销售有限公司", 400, 42, 200),
        line("金额 10.00", 0, 90), line("价税合计（小写） 10.00", 0, 120, 230),
    ]
    record = validate_record(parse_invoice(lines, (800, 600)))
    assert record.tax is None
    assert record.status == "待复核"
    assert "缺少税额" in record.validation_result


def test_missing_seller_name_requires_manual_review() -> None:
    lines = [
        line("合计 10.00 0.60", 0, 50, 180),
        line("价税合计（小写） 10.60", 0, 90, 230),
    ]
    record = validate_record(parse_invoice(lines, (800, 600)))
    assert record.seller_name is None
    assert "缺少销售方公司名称" in record.validation_result


def test_explicit_exemption_is_zero_tax_and_negative_invoice_is_allowed() -> None:
    exempt = [
        line("发票号码：123456789012", 0, 0, 180),
        line("销售方信息", 400, 20), line("名称：免税销售有限公司", 400, 42, 200),
        line("金额 44.05", 0, 90), line("不征税", 0, 120),
        line("价税合计（小写） ¥44.05", 0, 150, 240),
    ]
    exempt_record = validate_record(parse_invoice(exempt, (800, 600)))
    assert exempt_record.tax == Decimal("0.00")
    assert exempt_record.status == "成功"

    negative = [
        line("发票号码：123456789013", 0, 0, 180),
        line("销售方信息", 400, 20), line("名称：红字销售有限公司", 400, 42, 200),
        line("合计 -100.00 -6.00", 0, 100, 220),
        line("价税合计（小写） -106.00", 0, 140, 220),
    ]
    negative_record = validate_record(parse_invoice(negative, (800, 600)))
    assert negative_record.amount == Decimal("-100.00")
    assert negative_record.tax == Decimal("-6.00")
    assert negative_record.total == Decimal("-106.00")
    assert negative_record.status == "成功"


def test_small_total_value_is_selected_when_upper_and_lower_labels_share_a_row() -> None:
    lines = [
        line("销售方信息", 400, 20), line("名称：大小写销售有限公司", 400, 42, 220),
        line("金额 44.05", 0, 90), line("不征税", 0, 120),
        line("价税合计（大写）", 0, 150, 170), line("（小写）", 420, 150, 70),
        line("￥44.05", 520, 150, 80),
    ]
    record = parse_invoice(lines, (800, 600))
    assert record.total == Decimal("44.05")


def test_summary_values_split_across_lines_follow_their_printed_column_headers() -> None:
    lines = [
        line("销售方信息", 420, 10), line("名称：分栏销售有限公司", 420, 30, 200),
        line("金额", 120, 50, 45), line("税额", 320, 50, 45),
        line("合计", 0, 100, 50), line("10.00", 120, 105, 50), line("0.60", 320, 122, 50),
        line("价税合计（小写）", 0, 160, 150), line("10.60", 350, 160, 50),
    ]
    record = parse_invoice(lines, (800, 600))
    assert record.amount == Decimal("10.00")
    assert record.tax == Decimal("0.60")
    assert record.total == Decimal("10.60")


def test_amount_tax_mismatch_requires_review_and_preserves_values() -> None:
    lines = [
        line("销售方信息", 400, 20), line("名称：差异销售有限公司", 400, 42, 200),
        line("合计 100.00 6.00", 0, 100, 200), line("价税合计（小写） 107.00", 0, 140, 220),
    ]
    record = validate_record(parse_invoice(lines, (800, 600)))
    assert (record.amount, record.tax, record.total) == (Decimal("100.00"), Decimal("6.00"), Decimal("107.00"))
    assert record.status == "待复核"
    assert any("不一致" in item for item in record.validation_result)


def test_small_rounding_difference_of_one_cent_is_accepted() -> None:
    record = parse_invoice([
        line("发票号码：123456789014", 0, 0, 180),
        line("销售方信息", 400, 20), line("名称：舍入销售有限公司", 400, 42, 200),
        line("合计 1.00 0.01", 0, 100, 180), line("价税合计（小写） 1.02", 0, 140, 220),
    ], (800, 600))
    assert validate_record(record).status == "成功"


def test_duplicate_invoice_and_duplicate_image_are_flagged_not_removed() -> None:
    first = validate_record(parse_invoice(ordinary_lines(), (1000, 1000)))
    second = validate_record(parse_invoice(ordinary_lines(), (1000, 1000)))
    first.image_sha256 = second.image_sha256 = "same-image-hash"
    first.invoice_number = second.invoice_number = "same-invoice"
    mark_duplicates([first, second])
    assert first.status == second.status == "待复核"
    assert len([first, second]) == 2
    assert all(any("号码重复候选" in issue for issue in row.validation_result) for row in (first, second))
    assert all(any("重复图片" in issue for issue in row.validation_result) for row in (first, second))


def test_same_invoice_number_uses_seller_date_and_code_as_duplicate_evidence() -> None:
    from invoice_tool.models import InvoiceRecord

    left = InvoiceRecord(1, "a.jpg", "a.jpg", invoice_number="000123456789", invoice_code="1000", invoice_date="2026-01-01", seller_name="同一公司")
    matching = InvoiceRecord(2, "b.jpg", "b.jpg", invoice_number="000123456789", invoice_code="1000", invoice_date="2026-01-01", seller_name="同一公司")
    different_seller = InvoiceRecord(3, "c.jpg", "c.jpg", invoice_number="000123456789", invoice_code="1000", invoice_date="2026-01-01", seller_name="另一公司")
    for item in (left, matching, different_seller):
        validate_record(item)
    mark_duplicates([left, matching, different_seller])
    assert left.status == matching.status == different_seller.status == "待复核"
    assert any("号码重复候选" in issue for issue in left.validation_result)
    assert any("有差异" in issue for issue in different_seller.validation_result)


def test_rotated_and_low_confidence_boxes_keep_recognized_values() -> None:
    # Coordinates are tall/vertical and slightly skewed as in a rotated phone photo.
    lines = [
        OCRLine("销售方信息", ((30, 10), (42, 10), (44, 100), (32, 100)), 0.62),
        OCRLine("名称：旋转销售有限公司", ((55, 10), (67, 10), (69, 210), (57, 210)), 0.62),
        OCRLine("合计 50.00 3.00", ((100, 10), (112, 10), (114, 190), (102, 190)), 0.62),
        OCRLine("价税合计（小写）53.00", ((130, 10), (142, 10), (144, 220), (132, 220)), 0.62),
    ]
    record = parse_invoice(lines, (400, 800))
    assert record.amount == Decimal("50.00")
    assert record.tax == Decimal("3.00")
    assert record.total == Decimal("53.00")
    assert record.raw_ocr[0]["score"] == 0.62
