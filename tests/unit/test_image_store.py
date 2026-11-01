from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from house_account.image_store import InvalidImageError, store_image


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


@pytest.mark.parametrize(
    ("mode", "transparency"),
    [
        ("RGB", (0, 0, 0)),
        ("L", 0),
    ],
)
def test_png_transparency_key_is_flattened_on_white_for_ocr(
    tmp_path: Path,
    mode: str,
    transparency: int | tuple[int, int, int],
) -> None:
    image = Image.new(mode, (20, 10))
    if mode == "RGB":
        image.putdata(
            [(0, 0, 0) if x < 10 else (32, 32, 32) for _ in range(10) for x in range(20)]
        )
    else:
        image.putdata([0 if x < 10 else 32 for _ in range(10) for x in range(20)])
    buffer = BytesIO()
    image.save(buffer, format="PNG", transparency=transparency)
    content = buffer.getvalue()
    with Image.open(BytesIO(content)) as png_image:
        assert "transparency" in png_image.info

    stored = store_image(content, tmp_path / "original", tmp_path / "ocr")

    with Image.open(tmp_path / "ocr" / stored.ocr_image_name) as ocr_image:
        transparent_pixel_result = ocr_image.getpixel((2, 5))
        opaque_pixel_result = ocr_image.getpixel((17, 5))
        assert isinstance(transparent_pixel_result, tuple)
        assert isinstance(opaque_pixel_result, tuple)
        assert all(channel >= 250 for channel in transparent_pixel_result)
        assert all(channel <= 40 for channel in opaque_pixel_result)


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


def test_png_with_invalid_idat_crc_is_reported_as_invalid_image(tmp_path: Path) -> None:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), "white").save(buffer, format="PNG")
    content = bytearray(buffer.getvalue())
    offset = 8
    while offset < len(content):
        chunk_length = int.from_bytes(content[offset : offset + 4], "big")
        chunk_type = content[offset + 4 : offset + 8]
        if chunk_type == b"IDAT":
            crc_offset = offset + 8 + chunk_length
            content[crc_offset] ^= 0xFF
            break
        offset += 12 + chunk_length
    else:
        raise AssertionError("PNGにIDATチャンクがありません")

    with pytest.raises(InvalidImageError, match="破損していないJPEGまたはPNG"):
        store_image(bytes(content), tmp_path / "original", tmp_path / "ocr")
