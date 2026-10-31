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

    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    @property
    def database_path(self) -> Path:
        return self.data_dir / "house-account.sqlite3"

    @property
    def image_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def ocr_input_dir(self) -> Path:
        return self.data_dir / "ocr-inputs"

    @classmethod
    def from_env(cls) -> Self:
        return cls(
            data_dir=cls._configured_path("HOUSE_ACCOUNT_DATA_DIR", Path("data/app")),
            model_dir=cls._configured_path("HOUSE_ACCOUNT_MODEL_DIR", Path("artifacts/ocr-models")),
        )

    @staticmethod
    def _configured_path(variable: str, default: Path) -> Path:
        path = Path(os.environ.get(variable, str(default))).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()
