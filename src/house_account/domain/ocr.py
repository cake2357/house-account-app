import math
from dataclasses import dataclass

Point = tuple[float, float]


@dataclass(frozen=True)
class OcrBlock:
    text: str
    polygon: tuple[Point, ...]
    confidence: float

    def __post_init__(self) -> None:
        if len(self.polygon) != 4 or not all(
            math.isfinite(value) for point in self.polygon for value in point
        ):
            raise ValueError("OCR座標は有限数の4頂点が必要です。")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("OCR信頼度は0〜1で指定してください。")

    @property
    def left(self) -> float:
        return min(point[0] for point in self.polygon)

    @property
    def center_y(self) -> float:
        return sum(point[1] for point in self.polygon) / 4

    @property
    def center_x(self) -> float:
        return sum(point[0] for point in self.polygon) / 4

    @property
    def height(self) -> float:
        return max(point[1] for point in self.polygon) - min(point[1] for point in self.polygon)


@dataclass(frozen=True)
class OcrResult:
    blocks: tuple[OcrBlock, ...]
    models: dict[str, str]
    versions: dict[str, str]

    @property
    def raw_text(self) -> str:
        return "\n".join(block.text for block in self.blocks)


@dataclass(frozen=True)
class Candidate:
    id: str
    name: str
    raw_text: str
    block_indexes: tuple[int, ...]
    confidence: float
    needs_review: bool
    review_reasons: tuple[str, ...]
