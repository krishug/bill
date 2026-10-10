from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook

from benchmark.evaluate_accuracy import evaluate
from invoice_tool.batch import BatchProcessor
from invoice_tool.excel_export import export_excel
from invoice_tool.models import OCRLine


def invoice_lines() -> list[OCRLine]:
    def box(text: str, x: int, y: int, width: int = 120) -> OCRLine:
        return OCRLine(text, ((x, y), (x + width, y), (x + width, y + 12), (x, y + 12)), 0.95)

    return [
        box("销售方信息", 400, 10), box("名称：批次测试公司", 400, 30),
        box("发票号码：00009887766554412345", 0, 50, 240), box("合计 10.00 0.60", 0, 80),
        box("价税合计（小写）10.60", 0, 110, 210),
    ]


class FakeOCR:
    def recognize(self, path: str | Path):
        if Path(path).name == "bad.jpg":
            raise OSError("invalid image")
        return invoice_lines(), (800, 600)


def test_batch_stable_order_continues_after_invalid_image_and_flags_duplicates(tmp_path: Path) -> None:
    for name, contents in (("b.jpg", b"duplicate"), ("a.jpg", b"duplicate"), ("bad.jpg", b"not an image")):
        (tmp_path / name).write_bytes(contents)
    progress: list[tuple[int, int]] = []
    records = BatchProcessor(FakeOCR()).process_folder(
        tmp_path, progress=lambda current, total, _record: progress.append((current, total))
    )
    assert [record.original_filename for record in records] == ["a.jpg", "b.jpg", "bad.jpg"]
    assert [record.status for record in records] == ["待复核", "待复核", "失败"]
    assert all(any("重复图片" in issue for issue in record.validation_result) for record in records[:2])
    assert "invalid image" in records[-1].error
    assert progress == [(1, 3), (2, 3), (3, 3)]


def test_excel_has_three_sheets_numeric_money_filters_and_image_link(tmp_path: Path) -> None:
    (tmp_path / "one.png").write_bytes(b"image")
    records = BatchProcessor(FakeOCR()).process_folder(tmp_path)
    destination = export_excel(records, tmp_path / "result.xlsx")
    book = load_workbook(destination, data_only=True)
    assert book.sheetnames == ["发票明细", "异常复核", "处理统计", "OCR原始内容"]
    detail = book["发票明细"]
    assert detail.freeze_panes == "A2"
    assert detail.auto_filter.ref
    assert [detail.cell(1, column).value for column in range(1, 7)] == [
        "序号", "发票号码", "金额", "税额", "价税合计金额", "销售方公司名称",
    ]
    assert detail["B2"].value == "00009887766554412345"
    assert detail["B2"].data_type == "s"
    assert detail["C2"].value == 10
    assert detail["C2"].number_format == "0.00"
    assert detail["G2"].value == "one.png"
    assert '"amount":"10.00"' in detail["O2"].value
    assert book["处理统计"]["A2"].value == "发票记录数"
    assert book["处理统计"]["B2"].value == 1
    assert book["OCR原始内容"]["E2"].value == "销售方信息"
    assert book["OCR原始内容"]["F2"].value.startswith("[[")
    book.close()

    link_book = load_workbook(destination)
    assert link_book["发票明细"]["M2"].hyperlink is not None
    link_book.close()

    baseline = tmp_path / "baseline.csv"
    baseline.write_text(
        "filename,page_number,invoice_number,amount,tax,total,seller_name\n"
        "one.png,,00009887766554412345,10.00,0.60,10.60,批次测试公司\n",
        encoding="utf-8",
    )
    metrics = evaluate(baseline, destination)
    assert metrics == {field: (1, 1) for field in ("invoice_number", "amount", "tax", "total", "seller_name")}
