from pathlib import Path

import pytest

from house_account.config import PROJECT_ROOT, Settings


def test_relative_environment_paths_are_independent_of_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOUSE_ACCOUNT_DATA_DIR", "custom-data/receipts")
    monkeypatch.setenv("HOUSE_ACCOUNT_MODEL_DIR", "custom-models")
    monkeypatch.chdir(tmp_path)

    settings = Settings.from_env()

    assert settings.data_dir == (PROJECT_ROOT / "custom-data/receipts").resolve()
    assert settings.model_dir == (PROJECT_ROOT / "custom-models").resolve()
