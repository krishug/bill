from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Callable

from .models import InvoiceRecord
from .parser import parse_invoice
from .pdf_support import make_pdf_error_record, merge_pdf_continuation_pages, process_pdf_page
from .validation import mark_duplicates, validate_record

SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
SUPPORTED_SUFFIXES = SUPPORTED_IMAGE_SUFFIXES | {".pdf"}
ProgressCallback = Callable[[int, int, InvoiceRecord], None]


class BatchProcessor:
    def __init__(self, ocr_backend: object | None = None) -> None:
        self.ocr_backend = ocr_backend

    def process_folder(
        self,
        folder: str | Path,
        cancel_event: threading.Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> list[InvoiceRecord]:
        directory = Path(folder)
        return self.process_paths(discover_input_files(directory, recursive=True), cancel_event, progress)

    def process_paths(
        self,
        paths: list[str | Path],
        cancel_event: threading.Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> list[InvoiceRecord]:
        files = sorted(
            (Path(item) for item in paths if Path(item).is_file() and Path(item).suffix.lower() in SUPPORTED_SUFFIXES),
            key=lambda item: (str(item.resolve()).casefold(), str(item.resolve())),
        )
        if self.ocr_backend is None:
            from .ocr import PaddleOCRBackend

            self.ocr_backend = PaddleOCRBackend()

        records: list[InvoiceRecord] = []
        task_counts = [_pdf_task_count(path) if path.suffix.lower() == ".pdf" else 1 for path in files]
        total = sum(task_counts)
        current = 0
        index = 0
        for file_path, task_count in zip(files, task_counts):
            if cancel_event and cancel_event.is_set():
                break
            if file_path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES:
                index += 1
                record = _process_image(file_path, index, self.ocr_backend)
                records.append(record)
                current += 1
                if progress:
                    progress(current, total, record)
                continue

            try:
                import pymupdf

                document = pymupdf.open(file_path)
                if document.needs_pass:
                    document.close()
                    raise PermissionError("PDF 已加密，需要先解除密码保护后再识别")
                if document.page_count == 0:
                    document.close()
                    raise ValueError("PDF 不包含页面")
                for page_index in range(document.page_count):
                    if cancel_event and cancel_event.is_set():
                        break
                    index += 1
                    page_number = page_index + 1
                    try:
                        record = process_pdf_page(file_path, document.load_page(page_index), page_number, self.ocr_backend)
                    except Exception as exc:
                        record = make_pdf_error_record(
                            file_path, f"PDF 第 {page_number} 页处理失败：{type(exc).__name__}: {exc}", page_number
                        )
                    record.index = index
                    records.append(record)
                    current += 1
                    if progress:
                        progress(current, total, record)
                document.close()
            except Exception as exc:
                index += 1
                record = make_pdf_error_record(file_path, f"PDF 处理失败：{type(exc).__name__}: {exc}")
                record.index = index
                records.append(record)
                current += 1
                if progress:
                    progress(current, total, record)

        records = merge_pdf_continuation_pages(records)
        for index, record in enumerate(records, start=1):
            record.index = index
        mark_duplicates(records)
        return records


def discover_input_files(folder: str | Path, recursive: bool = True) -> list[Path]:
    directory = Path(folder)
    files: list[Path] = []
    if recursive:
        for current, directories, names in os.walk(directory):
            directories[:] = sorted(
                name for name in directories
                if name not in {".git", ".venv", "__pycache__", ".pytest_cache", ".paddleocr", ".paddlex", "models"}
                and not name.startswith(".")
            )
            for name in names:
                path = Path(current) / name
                if path.suffix.lower() in SUPPORTED_SUFFIXES:
                    files.append(path)
    else:
        files = [item for item in directory.iterdir() if item.is_file() and item.suffix.lower() in SUPPORTED_SUFFIXES]
    return sorted(files, key=lambda item: (str(item.relative_to(directory)).casefold(), str(item.relative_to(directory))))


def _pdf_task_count(path: Path) -> int:
    try:
        import pymupdf

        with pymupdf.open(path) as document:
            return max(1, document.page_count) if not document.needs_pass else 1
    except Exception:
        return 1


def _process_image(path: Path, index: int, backend: object) -> InvoiceRecord:
    record = InvoiceRecord(index=index, file_path=str(path.resolve()), original_filename=path.name)
    try:
        record.image_sha256 = _sha256(path)
        lines, size = backend.recognize(path)  # type: ignore[attr-defined]
        parsed = parse_invoice(lines, size)
        record.invoice_number = parsed.invoice_number
        record.invoice_code = parsed.invoice_code
        record.invoice_date = parsed.invoice_date
        record.invoice_number_issue = parsed.invoice_number_issue
        record.amount = parsed.amount
        record.tax = parsed.tax
        record.total = parsed.total
        record.seller_name = parsed.seller_name
        record.raw_text = parsed.raw_text
        record.raw_ocr = parsed.raw_ocr
        record.original_extracted_fields = parsed.as_cloud_fields()
        validate_record(record)
    except Exception as exc:  # A corrupt image must not stop its batch.
        record.error = f"{type(exc).__name__}: {exc}"
        validate_record(record)
    return record


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
