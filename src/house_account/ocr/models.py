import hashlib
import json
import shutil
import tarfile
import tempfile
from pathlib import Path
from urllib.request import urlopen

DETECTION_MODEL = "PP-OCRv5_server_det"
RECOGNITION_MODEL = "PP-OCRv5_server_rec"
MODEL_NAMES = (DETECTION_MODEL, RECOGNITION_MODEL)
MODEL_BASE_URL = (
    "https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0"
)
REQUIRED_MODEL_FILES = ("inference.json", "inference.pdiparams", "inference.yml")


class ModelsNotPreparedError(RuntimeError):
    pass


def check_models(model_dir: Path) -> None:
    missing = [
        f"{name}/{filename}"
        for name in MODEL_NAMES
        for filename in REQUIRED_MODEL_FILES
        if not (model_dir / name / filename).is_file()
    ]
    if missing:
        raise ModelsNotPreparedError(
            "OCRモデルが未準備です。uv run --locked python scripts/prepare_ocr_models.py "
            f"--model-dir {model_dir} を実行してください。"
        )


def prepare_models(model_dir: Path) -> dict[str, dict[str, str]]:
    """公式配布アーカイブだけを取得し、完成したモデル単位で配置する。"""
    model_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict[str, str]] = {}
    for name in MODEL_NAMES:
        destination = model_dir / name
        url = f"{MODEL_BASE_URL}/{name}_infer.tar"
        if not all((destination / filename).is_file() for filename in REQUIRED_MODEL_FILES):
            with tempfile.TemporaryDirectory(dir=model_dir) as temporary:
                temporary_dir = Path(temporary)
                archive_path = temporary_dir / "model.tar"
                with urlopen(url, timeout=60) as response, archive_path.open("wb") as output:
                    shutil.copyfileobj(response, output)
                with tarfile.open(archive_path) as archive:
                    archive.extractall(temporary_dir / "unpacked", filter="data")
                configs = list((temporary_dir / "unpacked").rglob("inference.yml"))
                if len(configs) != 1 or not all(
                    (configs[0].parent / filename).is_file() for filename in REQUIRED_MODEL_FILES
                ):
                    raise ValueError(f"モデルの内容が不正です: {name}")
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.move(str(configs[0].parent), destination)
        manifest[name] = {
            "url": url,
            **{
                filename: hashlib.sha256((destination / filename).read_bytes()).hexdigest()
                for filename in REQUIRED_MODEL_FILES
            },
        }
    check_models(model_dir)
    (model_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest
