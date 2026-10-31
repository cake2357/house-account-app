import os
from dataclasses import dataclass
from pathlib import Path
from typing import Self

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    model_dir: Path
    ocr_timeout_seconds: float = 300

    @classmethod
    def from_env(cls) -> Self:
        return cls(
            data_dir=Path(os.environ.get("HOUSE_ACCOUNT_DATA_DIR", PROJECT_ROOT / "data/app"))
            .expanduser()
            .resolve(),
            model_dir=Path(
                os.environ.get("HOUSE_ACCOUNT_MODEL_DIR", PROJECT_ROOT / "artifacts/ocr-models")
            )
            .expanduser()
            .resolve(),
        )
