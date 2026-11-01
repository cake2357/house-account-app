from __future__ import annotations

import io
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_OCR_EDGE = 4_000


class InvalidImageError(ValueError):
    pass


@dataclass(frozen=True)
class StoredImage:
    image_name: str
    ocr_image_name: str
    media_type: str


def _decode_image(content: bytes) -> tuple[Image.Image, str]:
    if not content:
        raise InvalidImageError("画像ファイルが空です。")
    if len(content) > MAX_IMAGE_BYTES:
        raise InvalidImageError("画像は15MiB以下にしてください。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                if image.format not in {"JPEG", "PNG"}:
                    raise InvalidImageError("JPEGまたはPNG画像を選択してください。")
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise InvalidImageError("画像は4,000万画素以下にしてください。")
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                image.load()
                return ImageOps.exif_transpose(image).copy(), image.format or ""
    except InvalidImageError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        ValueError,
    ) as error:
        raise InvalidImageError(
            "画像の内容を読み取れません。破損していないJPEGまたはPNGを選択してください。"
        ) from error


def store_image(
    content: bytes, storage_dir: Path, ocr_storage_dir: Path | None = None
) -> StoredImage:
    image, image_format = _decode_image(content)
    token = str(uuid.uuid4())
    ocr_storage_dir = ocr_storage_dir or storage_dir
    extension = ".jpg" if image_format == "JPEG" else ".png"
    image_name = f"{token}{extension}"
    ocr_image_name = f"{token}.jpg"
    storage_dir.mkdir(parents=True, exist_ok=True)
    ocr_storage_dir.mkdir(parents=True, exist_ok=True)
    original_path = storage_dir / image_name
    processed_path = ocr_storage_dir / ocr_image_name
    temporary_paths: list[Path] = []
    try:
        for path in (original_path, processed_path):
            temporary = path.with_name(f".{path.name}.tmp")
            temporary_paths.append(temporary)
        temporary_paths[0].write_bytes(content)
        prepared = ImageOps.exif_transpose(image)
        if prepared.width > MAX_OCR_EDGE or prepared.height > MAX_OCR_EDGE:
            prepared.thumbnail((MAX_OCR_EDGE, MAX_OCR_EDGE), Image.Resampling.LANCZOS)
        if prepared.mode not in {"RGB", "L"}:
            background = Image.new("RGB", prepared.size, "white")
            if "A" in prepared.getbands() or "transparency" in prepared.info:
                rgba_image = prepared.convert("RGBA")
                background.paste(rgba_image, mask=rgba_image.getchannel("A"))
                rgba_image.close()
                prepared = background
            else:
                prepared = prepared.convert("RGB")
        if prepared.mode == "L":
            prepared = prepared.convert("RGB")
        prepared.save(temporary_paths[1], format="JPEG", quality=92, optimize=True)
        temporary_paths[0].replace(original_path)
        temporary_paths[1].replace(processed_path)
    except BaseException:
        original_path.unlink(missing_ok=True)
        processed_path.unlink(missing_ok=True)
        raise
    finally:
        image.close()
        for path in temporary_paths:
            path.unlink(missing_ok=True)
    return StoredImage(
        image_name=image_name,
        ocr_image_name=ocr_image_name,
        media_type="image/jpeg" if image_format == "JPEG" else "image/png",
    )


def remove_orphaned_images(database_path: Path, storage_dir: Path) -> None:
    from house_account.database import connect

    if not storage_dir.exists():
        return
    with connect(database_path) as connection:
        referenced = {
            row[name]
            for row in connection.execute("SELECT image_name, ocr_image_name FROM receipts")
            for name in ("image_name", "ocr_image_name")
        }
    for path in storage_dir.iterdir():
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
            if path.name not in referenced:
                path.unlink(missing_ok=True)
        elif path.is_file() and path.name.endswith(".tmp"):
            path.unlink(missing_ok=True)
