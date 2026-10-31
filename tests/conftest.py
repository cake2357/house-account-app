from pathlib import Path

import pytest

from house_account.domain.ocr import OcrBlock


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures"


def make_block(
    text: str, y: float, x: float = 0, confidence: float = 0.99, height: float = 20
) -> OcrBlock:
    return OcrBlock(
        text=text,
        polygon=((x, y), (x + 120, y), (x + 120, y + height), (x, y + height)),
        confidence=confidence,
    )
