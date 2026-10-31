from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image

from house_account.image_store import store_image


def test_transparent_png_is_flattened_on_white_for_ocr(tmp_path: Path) -> None:
    buffer = BytesIO()
    Image.new("RGBA", (12, 8), (255, 0, 0, 0)).save(buffer, format="PNG")
    content = buffer.getvalue()
    original_dir = tmp_path / "original"
    ocr_dir = tmp_path / "ocr"

    stored = store_image(content, original_dir, ocr_dir)

    assert (original_dir / stored.image_name).read_bytes() == content
    with Image.open(ocr_dir / stored.ocr_image_name) as ocr_image:
        assert ocr_image.format == "JPEG"
        assert ocr_image.getpixel((5, 5)) == (255, 255, 255)


def test_exif_rotation_is_applied_to_separate_ocr_image(tmp_path: Path) -> None:
    buffer = BytesIO()
    image = Image.new("RGB", (20, 10), "white")
    exif = image.getexif()
    exif[274] = 6
    image.save(buffer, format="JPEG", exif=exif)
    content = buffer.getvalue()

    stored = store_image(content, tmp_path / "original", tmp_path / "ocr")

    assert (tmp_path / "original" / stored.image_name).read_bytes() == content
    with Image.open(tmp_path / "ocr" / stored.ocr_image_name) as ocr_image:
        assert ocr_image.size == (10, 20)
