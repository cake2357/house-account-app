import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from house_account.ocr.models import MODEL_NAMES, REQUIRED_MODEL_FILES, check_models, prepare_models


def test_prepare_models_installs_complete_directories_and_reuses_local_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloaded: list[str] = []

    def download(url: str, *, timeout: float) -> io.BytesIO:
        downloaded.append(url)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for filename in REQUIRED_MODEL_FILES:
                content = b"synthetic model"
                member = tarfile.TarInfo(f"model/{filename}")
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        buffer.seek(0)
        return buffer

    monkeypatch.setattr("house_account.ocr.models.urlopen", download)
    manifest = prepare_models(tmp_path)
    check_models(tmp_path)
    assert len(downloaded) == 2
    assert set(manifest) == set(MODEL_NAMES)
    for record in manifest.values():
        assert record["inference.json"] == hashlib.sha256(b"synthetic model").hexdigest()
    assert prepare_models(tmp_path) == manifest
    assert len(downloaded) == 2


def test_failed_download_does_not_leave_incomplete_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(url: str, *, timeout: float) -> io.BytesIO:
        raise OSError("取得失敗")

    monkeypatch.setattr("house_account.ocr.models.urlopen", fail)
    with pytest.raises(OSError, match="取得失敗"):
        prepare_models(tmp_path)
    assert list(tmp_path.iterdir()) == []
