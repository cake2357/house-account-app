from __future__ import annotations

import sqlite3
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import create_app
from house_account.config import Settings
from house_account.database import connect, initialize
from house_account.domain.ocr import OcrBlock, OcrResult
from house_account.ocr_queue import OcrExecutor
from house_account.service import ReceiptService


class FixedOcr:
    def __init__(self, fixture_dir: Path) -> None:
        self.fixture_dir = fixture_dir
        self.calls = 0

    def __call__(
        self, image_path: Path, timeout_seconds: float, stopping: threading.Event
    ) -> OcrResult:
        self.calls += 1
        if image_path.suffix != ".jpg":
            raise AssertionError("OCRには正規化済みJPEGを渡す")
        return OcrResult(
            blocks=(
                OcrBlock(
                    text="りんご 198",
                    polygon=((10, 10), (220, 10), (220, 30), (10, 30)),
                    confidence=0.98,
                ),
            ),
            models={"recognition": "fixed"},
            versions={"paddleocr": "test"},
        )


class WaitingOcr:
    def __call__(
        self, image_path: Path, timeout_seconds: float, stopping: threading.Event
    ) -> OcrResult:
        while not stopping.wait(0.02):
            pass
        raise InterruptedError


class EmptyOcr:
    def __call__(
        self, image_path: Path, timeout_seconds: float, stopping: threading.Event
    ) -> OcrResult:
        return OcrResult(blocks=(), models={}, versions={})


class FailedOcr:
    def __call__(
        self, image_path: Path, timeout_seconds: float, stopping: threading.Event
    ) -> OcrResult:
        raise RuntimeError("テスト用の推論失敗")


class TimeoutOcr:
    def __call__(
        self, image_path: Path, timeout_seconds: float, stopping: threading.Event
    ) -> OcrResult:
        raise TimeoutError


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", model_dir=tmp_path / "models")


@pytest.fixture
def image_bytes(fixture_dir: Path) -> bytes:
    return (fixture_dir / "synthetic_receipt.png").read_bytes()


def upload(client: TestClient, image_bytes: bytes) -> str:
    response = client.post(
        "/api/receipts",
        files={"file": ("receipt.png", image_bytes, "image/png")},
    )
    assert response.status_code == 201, response.text
    return cast(str, response.json()["receipt_id"])


def wait_for_run(client: TestClient, receipt_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(f"/api/receipts/{receipt_id}")
        assert response.status_code == 200, response.text
        run = response.json()["latest_run"]
        if run["status"] not in {"queued", "running"}:
            return cast(dict[str, Any], run)
        time.sleep(0.02)
    raise AssertionError("OCRが3秒以内に終了しませんでした")


def test_receipt_survives_restart_and_serves_image(
    settings: Settings, image_bytes: bytes, fixture_dir: Path
) -> None:
    executor = FixedOcr(fixture_dir)
    with TestClient(create_app(settings, executor), base_url="http://127.0.0.1") as client:
        receipt_id = upload(client, image_bytes)
        run = wait_for_run(client, receipt_id)
        assert run["status"] == "succeeded"
        assert run["raw"]["raw_text"] == "りんご 198"
        assert run["candidates"][0]["name"] == "りんご"
        image = client.get(f"/api/receipts/{receipt_id}/image")
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/png"
        with Image.open(next(settings.ocr_input_dir.glob("*.jpg"))) as ocr_image:
            assert ocr_image.format == "JPEG"
            assert max(ocr_image.size) <= 4_000
        assert len(client.get("/api/receipts").json()["items"]) == 1

    with TestClient(create_app(settings, executor), base_url="http://127.0.0.1") as client:
        detail = client.get(f"/api/receipts/{receipt_id}").json()
        assert detail["latest_run"]["status"] == "succeeded"
        assert detail["items"] == []
        assert client.get(f"/api/receipts/{receipt_id}/image").content == image_bytes
        assert executor.calls == 1


def test_tag_search_keeps_exact_match_within_result_limit(settings: Settings) -> None:
    with TestClient(create_app(settings, EmptyOcr()), base_url="http://127.0.0.1") as client:
        service = ReceiptService(settings)
        for number in range(1, 101):
            service.create_tag(f"A{number:03}食品")
        exact_tag = service.create_tag("食品")

        response = client.get("/api/tags", params={"q": "食品"})

    assert response.status_code == 200
    results = response.json()["items"]
    assert len(results) == 100
    assert results[0] == exact_tag


def test_manual_tags_revision_conflicts_and_receipt_scoping(
    settings: Settings, image_bytes: bytes, fixture_dir: Path
) -> None:
    with TestClient(
        create_app(settings, FixedOcr(fixture_dir)), base_url="http://127.0.0.1"
    ) as client:
        receipt_id = upload(client, image_bytes)
        run = wait_for_run(client, receipt_id)
        candidate = run["candidates"][0]
        tag = client.post("/api/tags", json={"name": "食品"})
        assert tag.status_code == 201
        duplicate = client.post("/api/tags", json={"name": " 食品 "})
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["existing_tag"]["id"] == tag.json()["id"]

        normalized_tag = client.post("/api/tags", json={"name": "ﾊﾟﾝ"})
        assert normalized_tag.status_code == 201
        assert normalized_tag.json()["name"] == "パン"
        assert client.get("/api/tags", params={"q": "ﾊﾟﾝ"}).json()["items"] == [
            normalized_tag.json()
        ]
        duplicate_normalized_tag = client.post("/api/tags", json={"name": "パン"})
        assert duplicate_normalized_tag.status_code == 409
        assert (
            duplicate_normalized_tag.json()["error"]["existing_tag"]["id"]
            == normalized_tag.json()["id"]
        )

        saved = client.put(
            f"/api/receipts/{receipt_id}/items",
            json={
                "revision": 0,
                "status": "confirmed",
                "items": [
                    {
                        "id": None,
                        "name": "りんご（確認済み）",
                        "tag_ids": [tag.json()["id"]],
                        "source_run_id": run["id"],
                        "source_candidate_id": candidate["id"],
                    }
                ],
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["revision"] == 1
        assert saved.json()["items"][0]["tags"] == [tag.json()]
        assert saved.json()["items"][0]["name"] == "りんご（確認済み）"

        retry = client.post(f"/api/receipts/{receipt_id}/ocr-runs")
        assert retry.status_code == 202
        assert retry.json()["created"] is True
        retry_run = client.get(f"/api/ocr-runs/{retry.json()['run_id']}")
        deadline = time.monotonic() + 3
        while retry_run.json()["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.02)
            retry_run = client.get(f"/api/ocr-runs/{retry.json()['run_id']}")
        assert retry_run.json()["status"] == "succeeded"
        assert (
            client.get(f"/api/receipts/{receipt_id}").json()["items"][0]["name"]
            == "りんご（確認済み）"
        )

        stale = client.put(
            f"/api/receipts/{receipt_id}/items",
            json={"revision": 0, "status": "draft", "items": []},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "revision_conflict"
        assert (
            client.get(f"/api/receipts/{receipt_id}").json()["items"][0]["name"]
            == "りんご（確認済み）"
        )

        other_id = upload(client, image_bytes)
        wait_for_run(client, other_id)
        foreign = client.put(
            f"/api/receipts/{other_id}/items",
            json={
                "revision": 0,
                "status": "draft",
                "items": [{"id": saved.json()["items"][0]["id"], "name": "混入", "tag_ids": []}],
            },
        )
        assert foreign.status_code == 422
        assert client.get(f"/api/receipts/{other_id}").json()["items"] == []


def test_image_validation_and_uniform_errors(
    settings: Settings, image_bytes: bytes, fixture_dir: Path
) -> None:
    with TestClient(
        create_app(settings, FixedOcr(fixture_dir)), base_url="http://127.0.0.1"
    ) as client:
        invalid = client.post(
            "/api/receipts", files={"file": ("receipt.png", b"not an image", "image/png")}
        )
        assert invalid.status_code == 422
        assert set(invalid.json()["error"]) >= {"code", "message", "fields"}

        too_large = client.post(
            "/api/receipts",
            files={"file": ("receipt.png", image_bytes + b"0" * (15 * 1024 * 1024), "image/png")},
        )
        assert too_large.status_code == 422

        text_data = b"Comment\x00\x00" + zlib.compress(b"x" * (1024 * 1024 + 1))
        chunk_type = b"zTXt"
        crc = zlib.crc32(chunk_type + text_data)
        text_chunk = (
            struct.pack(">I", len(text_data)) + chunk_type + text_data + struct.pack(">I", crc)
        )
        ihdr_end = 8 + 12 + int.from_bytes(image_bytes[8:12], "big")
        oversized_text = image_bytes[:ihdr_end] + text_chunk + image_bytes[ihdr_end:]
        invalid_metadata = client.post(
            "/api/receipts",
            files={"file": ("receipt.png", oversized_text, "image/png")},
        )
        assert invalid_metadata.status_code == 422
        assert invalid_metadata.json()["error"]["code"] == "invalid_image"

        cross_origin = client.post(
            "/api/tags",
            headers={"Origin": "http://example.com"},
            json={"name": "危険"},
        )
        assert cross_origin.status_code == 403
        assert cross_origin.json()["error"]["code"] == "cross_origin"


def test_delete_refuses_active_ocr_then_removes_persisted_files(
    settings: Settings, image_bytes: bytes
) -> None:
    with TestClient(create_app(settings, WaitingOcr()), base_url="http://127.0.0.1") as client:
        receipt_id = upload(client, image_bytes)
        deadline = time.monotonic() + 2
        run: dict[str, object] | None = None
        while time.monotonic() < deadline:
            run = client.get(f"/api/receipts/{receipt_id}").json()["latest_run"]
            if run["status"] == "running":
                break
            time.sleep(0.01)
        assert run is not None and run["status"] == "running"
        duplicate_run = client.post(f"/api/receipts/{receipt_id}/ocr-runs")
        assert duplicate_run.status_code == 202
        assert duplicate_run.json() == {"run_id": run["id"], "created": False}
        blocked = client.delete(f"/api/receipts/{receipt_id}?revision=0")
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "ocr_running"

    with TestClient(
        create_app(settings, FixedOcr(Path("."))), base_url="http://127.0.0.1"
    ) as client:
        detail = client.get(f"/api/receipts/{receipt_id}").json()
        assert detail["latest_run"]["status"] == "interrupted"
        assert client.delete(f"/api/receipts/{receipt_id}?revision=0").status_code == 204
        assert client.get(f"/api/receipts/{receipt_id}").status_code == 404
        assert not list(settings.image_dir.glob("*.png"))
        assert not list(settings.ocr_input_dir.glob("*.jpg"))


def test_database_startup_marks_previously_running_run_interrupted(
    settings: Settings, image_bytes: bytes, fixture_dir: Path
) -> None:
    with TestClient(
        create_app(settings, FixedOcr(fixture_dir)), base_url="http://127.0.0.1"
    ) as client:
        receipt_id = upload(client, image_bytes)
        wait_for_run(client, receipt_id)
    with connect(settings.database_path) as connection:
        connection.execute("UPDATE ocr_runs SET status='running' WHERE receipt_id=?", (receipt_id,))
    with TestClient(
        create_app(settings, FixedOcr(fixture_dir)), base_url="http://127.0.0.1"
    ) as client:
        run = client.get(f"/api/receipts/{receipt_id}").json()["latest_run"]
        assert run["status"] == "interrupted"
        assert run["raw"]["raw_text"] == "りんご 198"


def test_second_process_cannot_open_same_data_directory(
    settings: Settings, fixture_dir: Path
) -> None:
    application = create_app(settings, FixedOcr(fixture_dir))
    with TestClient(application, base_url="http://127.0.0.1"):
        with pytest.raises(RuntimeError, match="別のアプリが使用中"):
            with TestClient(
                create_app(settings, FixedOcr(fixture_dir)), base_url="http://127.0.0.1"
            ):
                pass


@pytest.mark.parametrize(
    ("executor", "expected_status"),
    [(EmptyOcr(), "no_candidates"), (FailedOcr(), "ocr_failed"), (TimeoutOcr(), "timeout")],
)
def test_ocr_terminal_statuses(
    settings: Settings, image_bytes: bytes, executor: OcrExecutor, expected_status: str
) -> None:
    with TestClient(create_app(settings, executor), base_url="http://127.0.0.1") as client:
        receipt_id = upload(client, image_bytes)
        run = wait_for_run(client, receipt_id)
        assert run["status"] == expected_status


def test_extraction_failure_keeps_raw_ocr_result(
    settings: Settings, image_bytes: bytes, fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_extraction(_: OcrResult) -> tuple[()]:
        raise RuntimeError("候補抽出に失敗")

    monkeypatch.setattr("house_account.ocr_queue.extract_candidates", fail_extraction)
    with TestClient(
        create_app(settings, FixedOcr(fixture_dir)), base_url="http://127.0.0.1"
    ) as client:
        receipt_id = upload(client, image_bytes)
        run = wait_for_run(client, receipt_id)
        assert run["status"] == "extraction_failed"
        assert run["raw"]["raw_text"] == "りんご 198"
        assert run["candidates"] is None


def test_save_transaction_rolls_back_all_items_on_insert_failure(
    settings: Settings, image_bytes: bytes, fixture_dir: Path
) -> None:
    with TestClient(
        create_app(settings, FixedOcr(fixture_dir)), base_url="http://127.0.0.1"
    ) as client:
        receipt_id = upload(client, image_bytes)
        wait_for_run(client, receipt_id)
        with connect(settings.database_path) as connection:
            connection.execute(
                "CREATE TRIGGER reject_test_item BEFORE INSERT ON receipt_items "
                "WHEN NEW.name='失敗行' BEGIN SELECT RAISE(ABORT, 'test rollback'); END"
            )
        service = ReceiptService(settings)
        with pytest.raises(sqlite3.IntegrityError, match="test rollback"):
            service.save_items(
                receipt_id,
                0,
                "draft",
                [
                    {"name": "保存前の行", "tag_ids": []},
                    {"name": "失敗行", "tag_ids": []},
                ],
            )
        detail = service.get_receipt(receipt_id)
        assert detail["items"] == []
        assert detail["revision"] == 0


def test_migration_is_idempotent(settings: Settings) -> None:
    initialize(settings.database_path)
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations")]
    assert versions == [1]
