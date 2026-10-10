from decimal import Decimal
import json

from invoice_tool import cloud
from invoice_tool.cloud import VisionConfig, cloud_fallback_needed, extract_with_vision
from invoice_tool.models import InvoiceRecord, parse_decimal, record_from_cloud, validate_invoice_number


def test_decimal_parsing_does_not_treat_blank_as_zero() -> None:
    assert parse_decimal("") is None
    assert parse_decimal("-1,234.50") == Decimal("-1234.50")
    assert parse_decimal("￥0.00") == Decimal("0.00")
    assert parse_decimal("1,2,3.00") is None
    assert parse_decimal("NaN") is None


def test_invoice_number_lengths_are_validated_without_truncation() -> None:
    assert validate_invoice_number("00123456") == ("00123456", "")
    assert validate_invoice_number("00001234567890123456") == ("00001234567890123456", "")
    record = record_from_cloud(1, "invoice.jpg", {"invoice_number": "123456789012345678901"})
    assert record.invoice_number is None
    assert "位数异常" in record.invoice_number_issue


def test_cloud_is_disabled_without_explicit_api_key(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("INVOICE_VISION_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    monkeypatch.setattr(cloud, "LOCAL_ENV_PATH", tmp_path / ".env")
    assert VisionConfig.from_environment() is None


def test_other_service_credentials_do_not_enable_invoice_upload(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("INVOICE_VISION_API_KEY", raising=False)
    monkeypatch.setenv("OPENCODE_API_KEY", "not-an-invoice-vision-key")
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    monkeypatch.setattr(cloud, "LOCAL_ENV_PATH", tmp_path / ".env")
    assert VisionConfig.from_environment() is None


def test_cloud_endpoint_can_be_configured_without_enabling_automatic_upload(monkeypatch) -> None:
    monkeypatch.setenv("INVOICE_VISION_API_KEY", "test-key")
    monkeypatch.setenv("INVOICE_VISION_BASE_URL", "https://example.invalid/v1/")
    monkeypatch.setenv("INVOICE_VISION_MODEL", "vision-model")
    config = VisionConfig.from_environment()
    assert config is not None
    assert config.base_url == "https://example.invalid/v1"
    assert config.model == "vision-model"


def test_opencode_go_key_uses_documented_vision_endpoint_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("INVOICE_VISION_API_KEY", raising=False)
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "go-key")
    monkeypatch.delenv("INVOICE_VISION_BASE_URL", raising=False)
    monkeypatch.delenv("INVOICE_VISION_MODEL", raising=False)
    monkeypatch.setattr(cloud, "LOCAL_ENV_PATH", tmp_path / ".env")
    config = VisionConfig.from_environment()
    assert config is not None
    assert config.base_url == "https://opencode.ai/zen/go/v1"
    assert config.model == "deepseek-v4-flash-vision-exp"


def test_dotenv_file_loads_go_key_and_environment_overrides_it(monkeypatch, tmp_path) -> None:
    local_env = tmp_path / ".env"
    local_env.write_text(
        '# local-only config\nOPENCODE_GO_API_KEY="file-key"\n'
        "INVOICE_VISION_BASE_URL=https://vision.example/v1/\nINVOICE_VISION_MODEL=file-model\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cloud, "LOCAL_ENV_PATH", local_env)
    for key in ("INVOICE_VISION_API_KEY", "OPENCODE_GO_API_KEY", "INVOICE_VISION_BASE_URL", "INVOICE_VISION_MODEL"):
        monkeypatch.delenv(key, raising=False)
    config = VisionConfig.from_environment()
    assert config is not None
    assert config.api_key == "file-key"
    assert config.base_url == "https://vision.example/v1"
    assert config.model == "file-model"

    monkeypatch.setenv("OPENCODE_GO_API_KEY", "environment-key")
    assert VisionConfig.from_environment().api_key == "environment-key"


def test_cloud_fallback_only_eligible_for_failures_or_field_issues() -> None:
    duplicate_only = InvoiceRecord(1, "a", "a", validation_result=["重复图片（保留记录，需人工确认）"])
    missing_field = InvoiceRecord(2, "b", "b", validation_result=["缺少税额"])
    image_failure = InvoiceRecord(3, "c", "c", error="invalid image")
    assert not cloud_fallback_needed(duplicate_only)
    assert cloud_fallback_needed(missing_field)
    assert cloud_fallback_needed(image_failure)


def test_cloud_request_uses_configured_endpoint_and_session_headers(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "invoice.jpg"
    image_path.write_bytes(b"image-bytes")
    config = VisionConfig("secret", "https://opencode.ai/zen/go/v1", "vision-model")
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": json.dumps({
                "invoice_number": None, "invoice_code": None, "invoice_date": None,
                "amount": "1.00", "tax": "0.00",
                "total": "1.00", "seller_name": "示例公司",
            })}}]}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {key.lower(): value for key, value in request.header_items()}
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(cloud.urllib.request, "urlopen", fake_urlopen)
    result = extract_with_vision(image_path, config, session_id="stable-batch-session")
    assert captured["url"] == "https://opencode.ai/zen/go/v1/chat/completions"
    assert captured["headers"]["x-opencode-session"] == "stable-batch-session"
    assert captured["headers"]["user-agent"] == "invoice-image-extractor/0.1.0"
    assert captured["body"]["model"] == "vision-model"
    assert captured["timeout"] == 90
    assert result["amount"] == "1.00"


def test_cloud_upload_renders_only_the_confirmed_pdf_page(tmp_path, monkeypatch) -> None:
    import base64
    import pymupdf

    source = tmp_path / "multipage.pdf"
    document = pymupdf.open()
    document.new_page(width=100, height=100).insert_text((10, 20), "page one")
    document.new_page(width=100, height=100).insert_text((10, 20), "page two")
    document.save(source)
    document.close()
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": json.dumps({
                "invoice_number": None, "invoice_code": None, "invoice_date": None,
                "amount": None, "tax": None, "total": None, "seller_name": None,
            })}}]}).encode()

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr(cloud.urllib.request, "urlopen", fake_urlopen)
    extract_with_vision(source, VisionConfig("secret", "https://example.invalid/v1", "vision"), page_number=2)
    data_url = captured["body"]["messages"][0]["content"][1]["image_url"]["url"]
    assert data_url.startswith("data:image/png;base64,")
    rendered = base64.b64decode(data_url.split(",", 1)[1])
    assert rendered.startswith(b"\x89PNG\r\n\x1a\n")
