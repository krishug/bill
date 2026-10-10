from pathlib import Path

from PIL import Image

from invoice_tool.ocr import PaddleOCRBackend


class DirectionAwareFakeEngine:
    def __init__(self) -> None:
        self.calls = 0

    def ocr(self, image, cls=False):
        self.calls += 1
        if self.calls == 1:  # Original, deceptively plausible but sparse.
            count, box_width, box_height = 5, 42, 9
        elif self.calls == 2:  # 90°
            count, box_width, box_height = 4, 45, 8
        elif self.calls == 3:  # 270°, full invoice is upright.
            count, box_width, box_height = 36, 56, 10
        else:  # 180°
            count, box_width, box_height = 28, 8, 26
        result = []
        for index in range(count):
            x, y = 10 + (index % 3) * 70, 10 + (index // 3) * 20
            polygon = [[x, y], [x + box_width, y], [x + box_width, y + box_height], [x, y + box_height]]
            result.append([polygon, ("字段金额100", 0.95)])
        return [result]


def test_sparse_first_pass_retries_orientation_and_keeps_both_coordinate_spaces(tmp_path: Path) -> None:
    image_path = tmp_path / "rotated.jpg"
    Image.new("RGB", (100, 200), "white").save(image_path)
    backend = PaddleOCRBackend()
    backend._engine = DirectionAwareFakeEngine()
    lines, size = backend.recognize(image_path)
    assert size == (100, 200)
    assert len(lines) == 36
    assert lines[0].layout_box is not None
    assert lines[0].box != lines[0].layout_box
