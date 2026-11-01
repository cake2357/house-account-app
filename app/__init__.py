from __future__ import annotations

import fcntl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, cast
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from house_account.config import Settings
from house_account.database import initialize, recover_interrupted
from house_account.errors import AppError
from house_account.image_store import MAX_IMAGE_BYTES, remove_orphaned_images
from house_account.ocr_queue import OcrExecutor, OcrQueue
from house_account.service import ReceiptService

APP_DIR = Path(__file__).resolve().parent
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", "testserver"}


class ItemPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    tag_ids: list[str] = Field(default_factory=list, max_length=20)
    source_run_id: str | None = None
    source_candidate_id: str | None = None


class SaveItemsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    status: str
    items: list[ItemPayload] = Field(max_length=200)


class TagPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)


def _acquire_lock(data_dir: Path) -> Any:
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_file = (data_dir / "app.lock").open("a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock_file.close()
        raise RuntimeError(
            f"保存先 {data_dir} は別のアプリが使用中です。既存プロセスを終了してください。"
        ) from error
    return lock_file


def _error_payload(error: AppError) -> dict[str, Any]:
    return {
        "error": {
            "code": error.code,
            "message": error.message,
            "fields": error.fields,
            **error.extra,
        }
    }


def create_app(
    settings: Settings | None = None, ocr_executor: OcrExecutor | None = None
) -> FastAPI:
    app_settings = settings or Settings.from_env()
    service = ReceiptService(app_settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        lock_file = _acquire_lock(app_settings.data_dir)
        queue: OcrQueue | None = None
        try:
            initialize(app_settings.database_path)
            recover_interrupted(app_settings.database_path)
            remove_orphaned_images(app_settings.database_path, app_settings.image_dir)
            remove_orphaned_images(app_settings.database_path, app_settings.ocr_input_dir)
            queue = OcrQueue(service, app_settings, ocr_executor)
            application.state.service = service
            application.state.ocr_queue = queue
            queue.start()
            yield
        finally:
            if queue is not None:
                queue.stop()
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    application = FastAPI(title="家計簿アプリ", lifespan=lifespan)
    application.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=APP_DIR / "templates")

    @application.middleware("http")
    async def local_request_security(request: Request, call_next: Any) -> Response:
        hostname = (request.url.hostname or "").lower()
        if hostname not in ALLOWED_HOSTS:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_host",
                        "message": "許可されていないHostです。",
                        "fields": {},
                    }
                },
            )
        origin = request.headers.get("origin")
        if origin and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            try:
                parsed = urlsplit(origin)
                expected_port = request.url.port or (443 if request.url.scheme == "https" else 80)
                origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
                same_origin = (
                    parsed.scheme == request.url.scheme
                    and (parsed.hostname or "").lower() == hostname
                    and origin_port == expected_port
                )
            except ValueError:
                same_origin = False
            if not same_origin:
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "cross_origin",
                            "message": "同一オリジンから操作してください。",
                            "fields": {},
                        }
                    },
                )
        response = cast(Response, await call_next(request))
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
            "base-uri 'self'; frame-ancestors 'none'",
        )
        return response

    @application.exception_handler(AppError)
    async def app_error_handler(_: Request, error: AppError) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content=_error_payload(error))

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, error: RequestValidationError) -> JSONResponse:
        fields = {
            ".".join(str(part) for part in issue["loc"] if part != "body"): issue["msg"]
            for issue in error.errors()
        }
        wrapped = AppError(422, "validation_error", "入力内容を確認してください。", fields)
        return JSONResponse(status_code=422, content=_error_payload(wrapped))

    @application.exception_handler(StarletteHTTPException)
    async def http_error_handler(_: Request, error: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if error.status_code == 404 else "http_error"
        message = (
            "指定されたページが見つかりません。" if error.status_code == 404 else str(error.detail)
        )
        return JSONResponse(
            status_code=error.status_code,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "fields": {},
                }
            },
        )

    @application.get("/", response_class=HTMLResponse)
    async def receipt_list_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"page_title": "レシート一覧"},
        )

    @application.get("/receipts/{receipt_id}", response_class=HTMLResponse)
    async def receipt_detail_page(request: Request, receipt_id: str) -> HTMLResponse:
        service.get_receipt(receipt_id)
        return templates.TemplateResponse(
            request=request,
            name="receipt.html",
            context={"receipt_id": receipt_id, "page_title": "レシートの確認"},
        )

    @application.get("/api/receipts")
    async def list_receipts(page: int = Query(default=1, ge=1)) -> dict[str, Any]:
        return service.list_receipts(page)

    @application.post("/api/receipts", status_code=201)
    async def create_receipt(file: Annotated[UploadFile, File()]) -> dict[str, str]:
        content = await file.read(MAX_IMAGE_BYTES + 1)
        receipt_id, run_id = service.create_receipt(content)
        return {"receipt_id": receipt_id, "run_id": run_id}

    @application.get("/api/receipts/{receipt_id}")
    async def get_receipt(receipt_id: str) -> dict[str, Any]:
        return service.get_receipt(receipt_id)

    @application.get("/api/receipts/{receipt_id}/image")
    async def get_receipt_image(receipt_id: str) -> FileResponse:
        path = service.image_path(receipt_id)
        if not path.is_file():
            raise AppError(404, "not_found", "レシート画像が見つかりません。")
        media_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        return FileResponse(
            path, media_type=media_type, headers={"Cache-Control": "private, no-store"}
        )

    @application.post("/api/receipts/{receipt_id}/ocr-runs", status_code=202)
    async def request_ocr(receipt_id: str) -> dict[str, Any]:
        run_id, created = service.request_ocr(receipt_id)
        return {"run_id": run_id, "created": created}

    @application.get("/api/ocr-runs/{run_id}")
    async def get_ocr_run(run_id: str) -> dict[str, Any]:
        return service.get_run(run_id)

    @application.put("/api/receipts/{receipt_id}/items")
    async def save_items(receipt_id: str, payload: SaveItemsPayload) -> dict[str, Any]:
        return service.save_items(
            receipt_id,
            payload.revision,
            payload.status,
            [item.model_dump() for item in payload.items],
        )

    @application.get("/api/tags")
    async def list_tags(q: str = Query(default="", max_length=100)) -> dict[str, Any]:
        return {"items": service.list_tags(q)}

    @application.post("/api/tags", status_code=201)
    async def create_tag(payload: TagPayload) -> dict[str, str]:
        return service.create_tag(payload.name)

    @application.delete("/api/receipts/{receipt_id}", status_code=204)
    async def delete_receipt(receipt_id: str, revision: int = Query(ge=0)) -> Response:
        service.delete_receipt(receipt_id, revision)
        return Response(status_code=204)

    return application
