from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from .models import OCRLine


class PaddleOCRBackend:
    """Lazy-loaded local CPU PaddleOCR with automatic 90°/180°/270° selection."""

    def __init__(self) -> None:
        self._engine: Any = None

    def _get_engine(self) -> Any:
        if self._engine is None:
            from paddleocr import PaddleOCR

            self._engine = PaddleOCR(use_angle_cls=False, lang="ch", use_gpu=False, show_log=False)
        return self._engine

    @staticmethod
    def _normalize_result(result: Any, rotation: int, original_size: tuple[int, int]) -> list[OCRLine]:
        width, height = original_size
        normalized: list[OCRLine] = []
        if not result:
            return normalized
        first = result[0] if isinstance(result, list) and result else result
        for item in first or []:
            try:
                box, (text, score) = item
                points: list[tuple[float, float]] = []
                layout_points: list[tuple[float, float]] = []
                for point in box:
                    x, y = float(point[0]), float(point[1])
                    layout_points.append((x, y))
                    if rotation == 1:  # np.rot90: counter-clockwise
                        x, y = y, height - 1 - x
                    elif rotation == 2:
                        x, y = width - 1 - x, height - 1 - y
                    elif rotation == 3:  # clockwise
                        x, y = width - 1 - y, x
                    points.append((x, y))
                text = str(text).strip()
                if text:
                    normalized.append(
                        OCRLine(text=text, box=tuple(points), score=float(score), layout_box=tuple(layout_points))
                    )
            except (TypeError, ValueError, IndexError):
                continue
        return normalized

    @staticmethod
    def _orientation_score(lines: list[OCRLine]) -> float:
        score = 0.0
        for line in lines:
            if not line.box:
                continue
            box = line.layout_box or line.box
            xs = [point[0] for point in box]
            ys = [point[1] for point in box]
            width = max(xs) - min(xs)
            height = max(ys) - min(ys)
            horizontal = max(width, 1.0) / max(height, 1.0)
            # Horizontal text lines are the common invoice layout. The capped
            # ratio avoids one long line overwhelming all other evidence.
            score += min(len(line.text), 24) * line.score * min(horizontal, 4.0)
        return score

    def recognize(self, path: str | Path) -> tuple[list[OCRLine], tuple[int, int]]:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
        return self.recognize_image(image)

    def recognize_image(self, source: Image.Image) -> tuple[list[OCRLine], tuple[int, int]]:
        image = ImageOps.exif_transpose(source).convert("RGB")
        original_size = image.size
        engine = self._get_engine()

        def run(rotation: int) -> list[OCRLine]:
            prepared = image.rotate(rotation * 90, expand=True) if rotation else image
            # PaddleOCR accepts BGR arrays. Each inference retains its own boxes;
            # the normalizer maps them back into the original image coordinate space.
            import numpy as np

            bgr = np.asarray(prepared)[:, :, ::-1].copy()
            result = engine.ocr(bgr, cls=False)
            return self._normalize_result(result, rotation, original_size)

        base = run(0)
        base_score = self._orientation_score(base)
        # Only spend extra inferences when the detected text is predominantly
        # vertical; ordinary upright receipts use a single pass.
        aspect_sum = 0.0
        weighted_count = 0
        for line in base:
            if line.box:
                box = line.layout_box or line.box
                xs = [point[0] for point in box]
                ys = [point[1] for point in box]
                ratio = (max(xs) - min(xs) + 1) / (max(ys) - min(ys) + 1)
                aspect_sum += ratio * max(1, len(line.text))
                weighted_count += max(1, len(line.text))
        mean_ratio = aspect_sum / weighted_count if weighted_count else 1.0
        recognized_characters = sum(len(line.text) for line in base)
        # Some sideways scans yield a handful of plausible horizontal words;
        # line aspect alone mistakes those for a complete upright page. Retry
        # all quarter-turns when the first pass has too little invoice content.
        if mean_ratio >= 0.78 and len(base) >= 12 and recognized_characters >= 50:
            return base, original_size

        best = base
        best_score = base_score
        for rotation in (1, 3, 2):
            candidate = run(rotation)
            candidate_score = self._orientation_score(candidate)
            if candidate_score > best_score:
                best, best_score = candidate, candidate_score
        return best, original_size
