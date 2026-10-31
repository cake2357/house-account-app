from __future__ import annotations

import json
import sqlite3
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from house_account.config import Settings
from house_account.database import connect, now_iso, transaction
from house_account.errors import AppError, conflict, not_found
from house_account.image_store import InvalidImageError, store_image

PAGE_SIZE = 20
MAX_ITEMS = 200
MAX_ITEM_TAGS = 20


def normalize_tag_name(name: str) -> str:
    return unicodedata.normalize("NFKC", name).strip().casefold()


class ReceiptService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database_path = settings.database_path
        self.image_dir = settings.image_dir
        self.ocr_input_dir = settings.ocr_input_dir

    def create_receipt(self, content: bytes) -> tuple[str, str]:
        try:
            stored = store_image(content, self.image_dir, self.ocr_input_dir)
        except InvalidImageError as error:
            raise AppError(422, "invalid_image", str(error)) from error
        except OSError as error:
            raise AppError(500, "storage_failed", "画像を保存できませんでした。") from error
        receipt_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        now = now_iso()
        try:
            with connect(self.database_path) as connection, transaction(connection):
                connection.execute(
                    "INSERT INTO receipts(id, image_name, ocr_image_name, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (receipt_id, stored.image_name, stored.ocr_image_name, now, now),
                )
                connection.execute(
                    "INSERT INTO ocr_runs(id, receipt_id, status, created_at) "
                    "VALUES (?, ?, 'queued', ?)",
                    (run_id, receipt_id, now),
                )
        except BaseException:
            self._unlink_image(self.image_dir, stored.image_name)
            self._unlink_image(self.ocr_input_dir, stored.ocr_image_name)
            raise
        return receipt_id, run_id

    def list_receipts(self, page: int) -> dict[str, Any]:
        offset = (page - 1) * PAGE_SIZE
        with connect(self.database_path) as connection:
            total = connection.execute("SELECT count(*) FROM receipts").fetchone()[0]
            rows = connection.execute(
                "SELECT r.id, r.status, r.revision, r.created_at, r.updated_at, r.image_name, "
                "(SELECT status FROM ocr_runs o WHERE o.receipt_id=r.id "
                "ORDER BY o.created_at DESC LIMIT 1) AS ocr_status "
                "FROM receipts r ORDER BY r.created_at DESC, r.id DESC LIMIT ? OFFSET ?",
                (PAGE_SIZE, offset),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "page": page,
            "page_size": PAGE_SIZE,
            "total": total,
            "has_next": offset + len(rows) < total,
        }

    def get_receipt(self, receipt_id: str) -> dict[str, Any]:
        with connect(self.database_path) as connection:
            receipt = connection.execute(
                "SELECT id, status, revision, created_at, updated_at, image_name "
                "FROM receipts WHERE id=?",
                (receipt_id,),
            ).fetchone()
            if receipt is None:
                raise not_found()
            items = connection.execute(
                "SELECT i.id, i.name, i.position, i.source_run_id, i.source_candidate_id "
                "FROM receipt_items i WHERE i.receipt_id=? ORDER BY i.position",
                (receipt_id,),
            ).fetchall()
            item_data = []
            for item in items:
                tags = connection.execute(
                    "SELECT t.id, t.name FROM item_tags it JOIN tags t ON t.id=it.tag_id "
                    "WHERE it.item_id=? ORDER BY t.name COLLATE NOCASE, t.id",
                    (item["id"],),
                ).fetchall()
                item_data.append(
                    {
                        **dict(item),
                        "tag_ids": [tag["id"] for tag in tags],
                        "tags": [dict(tag) for tag in tags],
                    }
                )
            latest = connection.execute(
                "SELECT * FROM ocr_runs WHERE receipt_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (receipt_id,),
            ).fetchone()
            success = connection.execute(
                "SELECT * FROM ocr_runs WHERE receipt_id=? AND status IN ('succeeded','no_candidates') "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (receipt_id,),
            ).fetchone()
        return {
            **dict(receipt),
            "image_url": f"/api/receipts/{receipt_id}/image",
            "items": item_data,
            "latest_run": self._serialize_run(latest),
            "last_successful_run": self._serialize_run(success),
        }

    def image_path(self, receipt_id: str) -> Path:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT image_name FROM receipts WHERE id=?", (receipt_id,)
            ).fetchone()
        if row is None:
            raise not_found()
        return self.image_dir / str(row["image_name"])

    def request_ocr(self, receipt_id: str) -> tuple[str, bool]:
        run_id = str(uuid.uuid4())
        with connect(self.database_path) as connection, transaction(connection):
            if (
                connection.execute("SELECT 1 FROM receipts WHERE id=?", (receipt_id,)).fetchone()
                is None
            ):
                raise not_found()
            active = connection.execute(
                "SELECT id FROM ocr_runs WHERE receipt_id=? AND status IN ('queued','running') "
                "ORDER BY created_at DESC LIMIT 1",
                (receipt_id,),
            ).fetchone()
            if active:
                return active["id"], False
            connection.execute(
                "INSERT INTO ocr_runs(id, receipt_id, status, created_at) VALUES (?, ?, 'queued', ?)",
                (run_id, receipt_id, now_iso()),
            )
        return run_id, True

    def get_run(self, run_id: str) -> dict[str, Any]:
        with connect(self.database_path) as connection:
            row = connection.execute("SELECT * FROM ocr_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise not_found("OCR実行")
        return self._serialize_run(row) or {}

    @staticmethod
    def _serialize_run(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for field in ("raw_json", "candidates_json"):
            result[field.removesuffix("_json")] = (
                json.loads(result[field]) if result[field] is not None else None
            )
            del result[field]
        return result

    def save_items(
        self,
        receipt_id: str,
        revision: int,
        status: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if status not in {"draft", "confirmed"}:
            raise AppError(
                422,
                "validation_error",
                "保存状態を確認してください。",
                {"status": "不正な値です。"},
            )
        if len(items) > MAX_ITEMS:
            raise AppError(
                422,
                "validation_error",
                "明細は200件までです。",
                {"items": "件数が上限を超えています。"},
            )
        if status == "confirmed" and not items:
            raise AppError(
                422,
                "validation_error",
                "確認保存には1件以上の明細が必要です。",
                {"items": "明細を追加してください。"},
            )
        prepared: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_candidates: set[tuple[str, str]] = set()
        all_tag_ids: set[str] = set()
        for index, item in enumerate(items):
            name = str(item.get("name", "")).strip()
            if not 1 <= len(name) <= 200:
                raise AppError(
                    422,
                    "validation_error",
                    "商品名は1〜200文字で入力してください。",
                    {f"items.{index}.name": "長さを確認してください。"},
                )
            item_id = item.get("id")
            if item_id is not None:
                if not isinstance(item_id, str) or item_id in seen_ids:
                    raise AppError(
                        422,
                        "validation_error",
                        "明細IDが重複しています。",
                        {f"items.{index}.id": "IDを確認してください。"},
                    )
                seen_ids.add(item_id)
            tag_ids = item.get("tag_ids", [])
            if not isinstance(tag_ids, list) or any(
                not isinstance(tag_id, str) for tag_id in tag_ids
            ):
                raise AppError(
                    422,
                    "validation_error",
                    "タグIDが不正です。",
                    {f"items.{index}.tag_ids": "タグを選び直してください。"},
                )
            if len(tag_ids) > MAX_ITEM_TAGS or len(tag_ids) != len(set(tag_ids)):
                raise AppError(
                    422,
                    "validation_error",
                    "タグは明細ごとに20件まで指定できます。",
                    {f"items.{index}.tag_ids": "タグを確認してください。"},
                )
            all_tag_ids.update(tag_ids)
            source_run_id = item.get("source_run_id")
            source_candidate_id = item.get("source_candidate_id")
            if (source_run_id is None) != (source_candidate_id is None):
                raise AppError(
                    422,
                    "validation_error",
                    "OCR候補の参照が不完全です。",
                    {f"items.{index}": "候補を選び直してください。"},
                )
            if source_run_id is not None:
                pair = (str(source_run_id), str(source_candidate_id))
                if pair in seen_candidates:
                    raise AppError(
                        422,
                        "validation_error",
                        "同じOCR候補は一度だけ追加できます。",
                        {f"items.{index}": "重複した候補です。"},
                    )
                seen_candidates.add(pair)
            prepared.append(
                {
                    "id": item_id or str(uuid.uuid4()),
                    "name": name,
                    "tag_ids": tag_ids,
                    "source_run_id": source_run_id,
                    "source_candidate_id": source_candidate_id,
                }
            )
        with connect(self.database_path) as connection, transaction(connection):
            receipt = connection.execute(
                "SELECT revision FROM receipts WHERE id=?", (receipt_id,)
            ).fetchone()
            if receipt is None:
                raise not_found()
            if receipt["revision"] != revision:
                raise conflict(
                    "別の画面で内容が更新されました。最新内容を確認してください。",
                    "revision_conflict",
                )
            existing_ids = {
                row[0]
                for row in connection.execute(
                    "SELECT id FROM receipt_items WHERE receipt_id=?", (receipt_id,)
                )
            }
            if any(item["id"] not in existing_ids for item in prepared if item["id"] in seen_ids):
                raise AppError(
                    422,
                    "validation_error",
                    "このレシートの明細ではありません。",
                    {"items": "画面を再読み込みしてください。"},
                )
            if all_tag_ids:
                placeholders = ",".join("?" for _ in all_tag_ids)
                found_tags = {
                    row[0]
                    for row in connection.execute(
                        f"SELECT id FROM tags WHERE id IN ({placeholders})", tuple(all_tag_ids)
                    )
                }
                if found_tags != all_tag_ids:
                    raise AppError(
                        422,
                        "validation_error",
                        "存在しないタグが指定されています。",
                        {"items": "タグを選び直してください。"},
                    )
            for item in prepared:
                if item["source_run_id"] is not None:
                    run = connection.execute(
                        "SELECT receipt_id, candidates_json FROM ocr_runs WHERE id=? AND status IN ('succeeded','no_candidates')",
                        (item["source_run_id"],),
                    ).fetchone()
                    if run is None or run["receipt_id"] != receipt_id:
                        raise AppError(
                            422,
                            "validation_error",
                            "OCR候補の参照が不正です。",
                            {"items": "候補を選び直してください。"},
                        )
                    candidate_ids = {
                        candidate["id"] for candidate in json.loads(run["candidates_json"] or "[]")
                    }
                    if item["source_candidate_id"] not in candidate_ids:
                        raise AppError(
                            422,
                            "validation_error",
                            "OCR候補が見つかりません。",
                            {"items": "候補を選び直してください。"},
                        )
            connection.execute("DELETE FROM receipt_items WHERE receipt_id=?", (receipt_id,))
            for position, item in enumerate(prepared):
                connection.execute(
                    "INSERT INTO receipt_items(id, receipt_id, position, name, source_run_id, source_candidate_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        item["id"],
                        receipt_id,
                        position,
                        item["name"],
                        item["source_run_id"],
                        item["source_candidate_id"],
                    ),
                )
                connection.executemany(
                    "INSERT INTO item_tags(item_id, tag_id) VALUES (?, ?)",
                    [(item["id"], tag_id) for tag_id in item["tag_ids"]],
                )
            new_revision = revision + 1
            now = now_iso()
            connection.execute(
                "UPDATE receipts SET status=?, revision=?, updated_at=? WHERE id=?",
                (status, new_revision, now, receipt_id),
            )
        return self.get_receipt(receipt_id)

    def list_tags(self, query: str = "") -> list[dict[str, str]]:
        with connect(self.database_path) as connection:
            if query.strip():
                tags = connection.execute(
                    "SELECT id, name FROM tags WHERE name LIKE ? ESCAPE '\\' "
                    "ORDER BY name COLLATE NOCASE LIMIT 100",
                    (f"%{self._escape_like(query.strip())}%",),
                ).fetchall()
            else:
                tags = connection.execute(
                    "SELECT id, name FROM tags ORDER BY name COLLATE NOCASE LIMIT 100"
                ).fetchall()
        return [dict(tag) for tag in tags]

    def create_tag(self, name: str) -> dict[str, str]:
        display_name = unicodedata.normalize("NFKC", name).strip()
        if not 1 <= len(display_name) <= 40:
            raise AppError(
                422,
                "validation_error",
                "タグ名は1〜40文字で入力してください。",
                {"name": "長さを確認してください。"},
            )
        normalized = normalize_tag_name(display_name)
        tag_id = str(uuid.uuid4())
        try:
            with connect(self.database_path) as connection, transaction(connection):
                connection.execute(
                    "INSERT INTO tags(id, name, normalized_name, created_at) VALUES (?, ?, ?, ?)",
                    (tag_id, display_name, normalized, now_iso()),
                )
        except sqlite3.IntegrityError as error:
            with connect(self.database_path) as connection:
                existing = connection.execute(
                    "SELECT id, name FROM tags WHERE normalized_name=?", (normalized,)
                ).fetchone()
            raise AppError(
                409,
                "duplicate_tag",
                "同じ名前のタグが既にあります。",
                extra={"existing_tag": dict(existing) if existing else None},
            ) from error
        return {"id": tag_id, "name": display_name}

    def claim_next_run(self) -> dict[str, Any] | None:
        with connect(self.database_path) as connection, transaction(connection):
            row = connection.execute(
                "SELECT id, receipt_id FROM ocr_runs WHERE status='queued' "
                "ORDER BY created_at, rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            started = now_iso()
            connection.execute(
                "UPDATE ocr_runs SET status='running', started_at=? WHERE id=? AND status='queued'",
                (started, row["id"]),
            )
            receipt = connection.execute(
                "SELECT ocr_image_name FROM receipts WHERE id=?", (row["receipt_id"],)
            ).fetchone()
        return {
            "id": row["id"],
            "receipt_id": row["receipt_id"],
            "image_path": self.ocr_input_dir / receipt["ocr_image_name"],
        }

    def record_raw_result(self, run_id: str, raw: dict[str, Any]) -> None:
        with connect(self.database_path) as connection, transaction(connection):
            connection.execute(
                "UPDATE ocr_runs SET raw_json=? WHERE id=? AND status='running'",
                (json.dumps(raw, ensure_ascii=False), run_id),
            )

    def finish_run(
        self,
        run_id: str,
        status: str,
        candidates: list[dict[str, Any]] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with connect(self.database_path) as connection, transaction(connection):
            connection.execute(
                "UPDATE ocr_runs SET status=?, candidates_json=?, error_code=?, error_message=?, "
                "completed_at=? WHERE id=? AND status='running'",
                (
                    status,
                    json.dumps(candidates, ensure_ascii=False) if candidates is not None else None,
                    error_code,
                    error_message,
                    now_iso(),
                    run_id,
                ),
            )

    def mark_interrupted(self, run_id: str) -> None:
        with connect(self.database_path) as connection, transaction(connection):
            connection.execute(
                "UPDATE ocr_runs SET status='interrupted', error_code='interrupted', "
                "error_message='アプリの終了によりOCR処理が中断されました。', completed_at=? "
                "WHERE id=? AND status='running'",
                (now_iso(), run_id),
            )

    def delete_receipt(self, receipt_id: str, revision: int) -> None:
        with connect(self.database_path) as connection, transaction(connection):
            receipt = connection.execute(
                "SELECT image_name, ocr_image_name, revision FROM receipts WHERE id=?",
                (receipt_id,),
            ).fetchone()
            if receipt is None:
                raise not_found()
            if receipt["revision"] != revision:
                raise conflict(
                    "別の画面で内容が更新されました。最新内容を確認してください。",
                    "revision_conflict",
                )
            active = connection.execute(
                "SELECT 1 FROM ocr_runs WHERE receipt_id=? AND status IN ('queued','running') LIMIT 1",
                (receipt_id,),
            ).fetchone()
            if active:
                raise conflict("OCR処理が終わってから削除してください。", "ocr_running")
            connection.execute("DELETE FROM receipts WHERE id=?", (receipt_id,))
        self._unlink_image(self.image_dir, receipt["image_name"])
        self._unlink_image(self.ocr_input_dir, receipt["ocr_image_name"])

    @staticmethod
    def _unlink_image(directory: Path, name: str) -> None:
        path = directory / name
        if path.parent == directory:
            path.unlink(missing_ok=True)

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
