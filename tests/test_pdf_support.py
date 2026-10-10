from __future__ import annotations

from pathlib import Path

import pymupdf
from openpyxl import load_workbook

from invoice_tool.batch import BatchProcessor
from invoice_tool.excel_export import export_excel
from invoice_tool.models import OCRLine


def invoice_text(
    number: str = "00001234567890123456",
    seller: str = "甲方销售有限公司",
    date: str = "2026年10月12日",
    code: str = "110012345678",
    amount: str = "100.00",
    tax: str = "6.00",
    total: str = "106.00",
) -> list[str]:
    return [
        f"发票号码：{number}", f"发票代码：{code}", f"开票日期：{date}",
        "销售方信息", f"名称：{seller}", f"合计 {amount} {tax}",
        f"价税合计（小写）{total}",
    ]


def write_text_pdf(path: Path, pages: list[list[str]]) -> None:
    document = pymupdf.open()
    for rows in pages:
        page = document.new_page(width=600, height=800)
        for row, text in enumerate(rows):
            page.insert_text((40, 45 + row * 34), text, fontname="china-s", fontsize=12)
    document.save(path)
    document.close()


def invoice_ocr_lines(number: str = "00009876543212345678") -> list[OCRLine]:
    texts = [
        f"发票号码：{number}", "发票代码：330012345678", "开票日期：2026年10月13日",
        "销售方信息", "名称：乙方识别科技有限公司", "合计 50.00 3.00", "价税合计（小写）53.00",
    ]
    return [
        OCRLine(text, ((10, y * 30), (300, y * 30), (300, y * 30 + 18), (10, y * 30 + 18)), 0.96)
        for y, text in enumerate(texts, start=1)
    ]


class FakeOCR:
    def __init__(self, lines: list[OCRLine] | None = None) -> None:
        self.lines = lines if lines is not None else invoice_ocr_lines()
        self.calls: list[tuple[int, int]] = []

    def recognize(self, _path):
        return self.lines, (600, 800)

    def recognize_image(self, image):
        self.calls.append(image.size)
        return self.lines, image.size


def test_digital_pdf_uses_text_layer_and_preserves_invoice_number_as_text(tmp_path: Path) -> None:
    source = tmp_path / "electronic.pdf"
    write_text_pdf(source, [invoice_text(number="00000123456789012345")])
    backend = FakeOCR()
    records = BatchProcessor(backend).process_paths([source])
    assert len(records) == 1
    record = records[0]
    assert record.invoice_number == "00000123456789012345"
    assert record.invoice_code == "110012345678"
    assert record.invoice_date == "2026-10-12"
    assert record.source_page == 1
    assert record.source_pages == [1]
    assert record.recognition_source == "local PDF text"
    assert record.status == "成功"
    assert record.raw_ocr and record.raw_ocr[0]["score"] is None
    assert backend.calls == []  # A complete electronic page is not rasterized/OCRed.
    output = export_excel(records, tmp_path / "pdf-result.xlsx")
    workbook = load_workbook(output, read_only=True, data_only=True)
    sheet = workbook["发票明细"]
    assert sheet["B2"].value == "00000123456789012345"
    assert sheet["B2"].data_type == "s"
    assert sheet["G2"].value == "electronic.pdf"
    assert sheet["H2"].value == "1"
    workbook.close()


def test_scanned_pdf_page_uses_image_ocr_and_records_rendered_page(tmp_path: Path) -> None:
    source = tmp_path / "scan.pdf"
    image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 400), False)
    image.clear_with(255)
    document = pymupdf.open()
    page = document.new_page(width=600, height=800)
    page.insert_image(page.rect, pixmap=image)
    document.save(source)
    document.close()

    backend = FakeOCR(invoice_ocr_lines("00001234567890123456"))
    record = BatchProcessor(backend).process_paths([source])[0]
    assert record.invoice_number == "00001234567890123456"
    assert record.source_page == 1
    assert record.recognition_source == "local OCR (PDF page)"
    assert backend.calls == [(1200, 1600)]
    assert record.raw_ocr[0]["coordinate_unit"] == "pixel"


def test_incomplete_electronic_text_page_falls_back_to_ocr(tmp_path: Path) -> None:
    source = tmp_path / "partial-text-layer.pdf"
    write_text_pdf(source, [invoice_text()[:5]])  # Identifiers and seller, but no totals.
    backend = FakeOCR(invoice_ocr_lines("00001234567890123456"))
    record = BatchProcessor(backend).process_paths([source])[0]
    assert record.invoice_number == "00001234567890123456"
    assert record.amount is not None
    assert record.recognition_source == "local OCR (PDF page)"
    assert "[PDF第 1 页文字层]" in record.raw_text
    assert len(backend.calls) == 1


def test_mixed_pdf_chooses_text_or_ocr_per_page(tmp_path: Path) -> None:
    source = tmp_path / "mixed.pdf"
    document = pymupdf.open()
    electronic = document.new_page(width=600, height=800)
    for row, text in enumerate(invoice_text(number="00000111111122223333")):
        electronic.insert_text((40, 45 + row * 34), text, fontname="china-s", fontsize=12)
    scanned = document.new_page(width=600, height=800)
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 400), False)
    pixmap.clear_with(255)
    scanned.insert_image(scanned.rect, pixmap=pixmap)
    document.save(source)
    document.close()

    backend = FakeOCR()
    records = BatchProcessor(backend).process_paths([source])
    assert [record.source_page for record in records] == [1, 2]
    assert [record.recognition_source for record in records] == ["local PDF text", "local OCR (PDF page)"]
    assert len(backend.calls) == 1


def test_same_invoice_on_multiple_pdf_pages_is_merged_once(tmp_path: Path) -> None:
    source = tmp_path / "continuation.pdf"
    write_text_pdf(source, [invoice_text(), invoice_text()])
    records = BatchProcessor(FakeOCR()).process_paths([source])
    assert len(records) == 1
    assert records[0].source_pages == [1, 2]
    assert "PDF第 2 页" in records[0].raw_text


def test_multiple_invoice_numbers_on_a_page_are_not_split_or_guessed(tmp_path: Path) -> None:
    source = tmp_path / "two-on-one-page.pdf"
    write_text_pdf(source, [[
        "发票号码：00000123456789012345", "发票号码：00000765432109876543", "销售方信息",
        "名称：边界待复核有限公司", "合计 100.00 6.00", "价税合计（小写）106.00",
    ]])
    backend = FakeOCR(lines=[])
    record = BatchProcessor(backend).process_paths([source])[0]
    assert record.invoice_number is None
    assert record.amount is None
    assert record.status == "待复核"
    assert any("未自动拆分" in issue for issue in record.validation_result)


def test_repeated_invoice_sections_with_same_number_remain_for_boundary_review(tmp_path: Path) -> None:
    source = tmp_path / "repeated-same-number.pdf"
    write_text_pdf(source, [[
        "发票号码：00000123456789012345", "发票号码：00000123456789012345", "销售方信息",
        "名称：边界待复核有限公司", "合计 100.00 6.00", "价税合计（小写）106.00",
    ]])
    record = BatchProcessor(FakeOCR(lines=[])).process_paths([source])[0]
    assert record.invoice_number is None
    assert record.status == "待复核"
    assert any("标签" in issue and "未自动拆分" in issue for issue in record.validation_result)


def test_recursive_mixed_input_keeps_corrupt_pdf_failure_and_continues(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    write_text_pdf(nested / "valid.pdf", [invoice_text()])
    (tmp_path / "corrupt.pdf").write_bytes(b"not a pdf")
    (tmp_path / "image.jpg").write_bytes(b"fake image")
    records = BatchProcessor(FakeOCR()).process_folder(tmp_path)
    assert {record.original_filename for record in records} == {"valid.pdf", "corrupt.pdf", "image.jpg"}
    broken = next(record for record in records if record.original_filename == "corrupt.pdf")
    assert broken.status == "失败"
    assert "PDF" in broken.error
    assert len(records) == 3


def test_encrypted_pdf_is_reported_without_stopping_other_files(tmp_path: Path) -> None:
    source = tmp_path / "encrypted.pdf"
    write_text_pdf(source, [invoice_text()])
    document = pymupdf.open(source)
    protected = tmp_path / "protected.pdf"
    document.save(protected, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret")
    document.close()
    (tmp_path / "valid.pdf").write_bytes(source.read_bytes())
    records = BatchProcessor(FakeOCR()).process_paths([protected, tmp_path / "valid.pdf"])
    encrypted = next(record for record in records if record.original_filename == "protected.pdf")
    assert encrypted.status == "失败"
    assert "加密" in encrypted.error
    assert any(record.original_filename == "valid.pdf" for record in records)


def test_blank_pdf_page_is_reported_and_other_document_still_processes(tmp_path: Path) -> None:
    blank_path = tmp_path / "blank.pdf"
    blank = pymupdf.open()
    blank.new_page(width=100, height=100)
    blank.save(blank_path)
    blank.close()
    valid_path = tmp_path / "valid.pdf"
    write_text_pdf(valid_path, [invoice_text()])
    records = BatchProcessor(FakeOCR(lines=[])).process_paths([blank_path, valid_path])
    blank_record = next(record for record in records if record.original_filename == "blank.pdf")
    assert blank_record.status == "失败"
    assert "空白页" in blank_record.error
    assert any(record.original_filename == "valid.pdf" and record.status == "成功" for record in records)
