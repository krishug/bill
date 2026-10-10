from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import InvoiceRecord, record_from_cloud
from .validation import validate_record

OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
OPENCODE_GO_VISION_MODEL = "deepseek-v4-flash-vision-exp"
LOCAL_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def _read_local_env() -> dict[str, str]:
    """Read simple KEY=VALUE settings; the ignored .env is never logged."""
    settings: dict[str, str] = {}
    try:
        lines = LOCAL_ENV_PATH.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return settings
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if name:
            settings[name] = value
    return settings


def _setting(name: str, local: dict[str, str]) -> str:
    return os.getenv(name, "").strip() or local.get(name, "").strip()


@dataclass(frozen=True)
class VisionConfig:
    api_key: str
    base_url: str
    model: str

    @classmethod
    def from_environment(cls) -> "VisionConfig | None":
        # Use a dedicated variable so credentials configured for unrelated
        # services are never forwarded to the default endpoint by accident.
        local = _read_local_env()
        key = _setting("INVOICE_VISION_API_KEY", local) or _setting("OPENCODE_GO_API_KEY", local)
        if not key:
            return None
        return cls(
            api_key=key,
            base_url=(_setting("INVOICE_VISION_BASE_URL", local) or OPENCODE_GO_BASE_URL).rstrip("/"),
            model=_setting("INVOICE_VISION_MODEL", local) or OPENCODE_GO_VISION_MODEL,
        )


def cloud_fallback_needed(record: InvoiceRecord) -> bool:
    """Cloud fallback applies to OCR/image failures or unresolved field issues."""
    return bool(
        record.error
        or any(
            issue.startswith("缺少")
            or "不一致" in issue
            or "发票号码识别" in issue
            or "多个不同发票号码" in issue
            or "未找到唯一票面发票号码" in issue
            for issue in record.validation_result
        )
    )


def apply_cloud_result(record: InvoiceRecord, fields: dict[str, Any]) -> InvoiceRecord:
    """Apply a confirmed cloud result without hiding local/cloud disagreement."""
    cloud_record = record_from_cloud(record.index, record.file_path, fields)
    conflicts: list[str] = []

    if cloud_record.invoice_number is not None:
        if record.invoice_number and record.invoice_number != cloud_record.invoice_number:
            record.invoice_number = None
            record.invoice_number_issue = "本地与云端发票号码不一致，号码已留空待人工核对"
            conflicts.append(record.invoice_number_issue)
        else:
            record.invoice_number = cloud_record.invoice_number
            record.invoice_number_issue = ""
    elif not record.invoice_number:
        record.invoice_number_issue = (
            cloud_record.invoice_number_issue or "云端未返回可用的发票号码，留空待人工复核"
        )
    elif cloud_record.invoice_number_issue:
        conflicts.append("云端发票号码格式异常，保留本地号码并待人工复核")

    for field_name, label in (
        ("invoice_code", "发票代码"),
        ("invoice_date", "开票日期"),
        ("amount", "金额"),
        ("tax", "税额"),
        ("total", "价税合计"),
        ("seller_name", "销售方公司名称"),
    ):
        local_value = getattr(record, field_name)
        cloud_value = getattr(cloud_record, field_name)
        if cloud_value is None:
            continue  # A null cloud field never erases an existing local value.
        if local_value is not None and local_value != cloud_value:
            conflicts.append(f"本地与云端{label}不一致，需人工确认")
        setattr(record, field_name, cloud_value)

    record.recognition_source = cloud_record.recognition_source
    record.error = ""
    validate_record(record)
    for issue in conflicts:
        if issue not in record.validation_result:
            record.validation_result.append(issue)
    if conflicts and record.status != "失败":
        record.status = "待复核"
    return record


def extract_with_vision(
    image_path: str | Path,
    config: VisionConfig,
    session_id: str | None = None,
    page_number: int | None = None,
) -> dict[str, Any]:
    """Call an OpenAI-compatible vision endpoint; caller must obtain consent first."""
    suffix = Path(image_path).suffix.lower()
    if suffix == ".pdf":
        if page_number is None or page_number < 1:
            raise ValueError("上传 PDF 时必须指定页码")
        import pymupdf

        with pymupdf.open(image_path) as document:
            if document.needs_pass or page_number > document.page_count:
                raise ValueError("PDF 无法读取或页码不存在")
            pixmap = document.load_page(page_number - 1).get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
            image_bytes = pixmap.tobytes("png")
        mime = "image/png"
    else:
        image_bytes = Path(image_path).read_bytes()
        mime = "image/png" if suffix == ".png" else "image/jpeg"
    data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    prompt = (
        "读取这张中国发票图片，只提取票面原文，不推算或补全。严格返回 JSON 对象，字段为："
        "invoice_number(发票号码字符串或null), invoice_code(发票代码字符串或null), "
        "invoice_date(开票日期ISO字符串或null), amount(金额十进制字符串或null), "
        "tax(税额十进制字符串或null), total(价税合计十进制字符串或null), "
        "seller_name(销售方公司名称或null)。免税/不征税时，仅票面明确为零才返回税额0。"
    )
    body = {
        "model": config.model,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
    }
    request = urllib.request.Request(
        f"{config.base_url}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "invoice-image-extractor/0.1.0",
            "x-opencode-session": session_id or str(uuid.uuid4()),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        result = json.loads(response.read().decode("utf-8"))
    content = result["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(content).strip(), flags=re.IGNORECASE)
    parsed = json.loads(content)
    required = {"invoice_number", "invoice_code", "invoice_date", "amount", "tax", "total", "seller_name"}
    if not isinstance(parsed, dict) or not required.issubset(parsed):
        raise ValueError("云端返回缺少必要字段")
    return parsed
