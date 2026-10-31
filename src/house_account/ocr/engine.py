import os
from importlib.metadata import version
from pathlib import Path
from typing import Protocol

from house_account.domain.ocr import OcrBlock, OcrResult
from house_account.ocr.models import DETECTION_MODEL, RECOGNITION_MODEL, check_models


class OcrEngine(Protocol):
    def recognize(self, image_path: Path) -> OcrResult: ...


class PaddleOcrEngine:
    def __init__(self, model_dir: Path) -> None:
        self.model_dir = model_dir.resolve()

    def recognize(self, image_path: Path) -> OcrResult:
        check_models(self.model_dir)
        # PaddleXのimport時の接続確認も無効にする。画像の外部送信は行わない。
        os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
        os.environ["PADDLE_PDX_CACHE_HOME"] = str(self.model_dir / ".cache")
        os.environ["PADDLE_HOME"] = str(self.model_dir / ".paddle")
        os.environ["MPLCONFIGDIR"] = str(self.model_dir / ".matplotlib")
        from paddleocr import PaddleOCR

        pipeline = PaddleOCR(
            text_detection_model_name=DETECTION_MODEL,
            text_detection_model_dir=str(self.model_dir / DETECTION_MODEL),
            text_recognition_model_name=RECOGNITION_MODEL,
            text_recognition_model_dir=str(self.model_dir / RECOGNITION_MODEL),
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            text_rec_score_thresh=0.0,
            device="cpu",
            enable_mkldnn=False,
        )
        blocks = []
        for page in pipeline.predict(str(image_path.resolve())):
            for text, score, polygon in zip(
                page["rec_texts"], page["rec_scores"], page["rec_polys"], strict=True
            ):
                blocks.append(
                    OcrBlock(
                        text=str(text),
                        confidence=float(score),
                        polygon=tuple((float(point[0]), float(point[1])) for point in polygon),
                    )
                )
        return OcrResult(
            blocks=tuple(blocks),
            models={"detection": DETECTION_MODEL, "recognition": RECOGNITION_MODEL},
            versions={name: version(name) for name in ("paddleocr", "paddlepaddle", "paddlex")},
        )
