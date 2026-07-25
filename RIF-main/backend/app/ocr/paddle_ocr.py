"""Local PaddleOCR adapter used only for scanned PDFs and image uploads.

The adapter intentionally imports PaddleOCR lazily. The FastAPI API can still start
and process native TXT/DOCX/text-PDF files when the optional OCR runtime has not
been installed. Scanned uploads return a controlled `ocr_failed` response instead
of a traceback.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class PaddleOcrRuntimeError(RuntimeError):
    """Raised when the local PaddleOCR runtime cannot process a file."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class PaddleOcrAdapter:
    """Version-tolerant local PaddleOCR wrapper for English text extraction."""

    def __init__(self, *, language: str = "en") -> None:
        self.language = language
        self._engine: Any | None = None

    def _get_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        # PaddlePaddle 3.3.x on some Windows CPU environments can fail inside
        # the oneDNN/PIR execution path.  These flags disable that optional
        # optimization before PaddleOCR imports PaddlePaddle. They keep OCR local
        # and trade a small amount of speed for reliable CPU execution.
        os.environ.setdefault("FLAGS_use_mkldnn", "0")
        os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")
        os.environ.setdefault("FLAGS_enable_pir_api", "0")

        try:
            from paddleocr import PaddleOCR  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on local optional install
            raise PaddleOcrRuntimeError(
                "paddleocr_not_installed",
                "PaddleOCR is required for scanned PDF and image OCR. Install the Phase 8 requirements, then retry.",
            ) from exc

        # PaddleOCR 3.x changed public APIs from 2.x. Start with the 3.x-friendly
        # constructor and fall back to the legacy constructor when an older release
        # is installed on a developer machine.
        try:
            self._engine = PaddleOCR(lang=self.language, device="cpu")
        except TypeError:  # pragma: no cover - version dependent
            try:
                self._engine = PaddleOCR(use_angle_cls=True, lang=self.language, use_gpu=False)
            except TypeError:
                self._engine = PaddleOCR(use_angle_cls=True, lang=self.language)
        except Exception as exc:  # pragma: no cover - runtime/model download dependent
            raise PaddleOcrRuntimeError(
                "paddleocr_initialization_failed",
                "PaddleOCR could not initialize its local OCR model. Check the local OCR installation and model download, then retry.",
            ) from exc
        return self._engine

    @staticmethod
    def _normalise_json(value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith(("{", "[")):
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    return value
        return value

    @classmethod
    def _collect_text(cls, value: Any) -> list[str]:
        """Collect recognised text from common PaddleOCR 2.x and 3.x result shapes."""

        value = cls._normalise_json(value)
        output: list[str] = []

        if value is None:
            return output
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        if isinstance(value, dict):
            # PaddleOCR 3.x commonly emits rec_texts. Keep other common variants
            # so small interface changes do not leak implementation errors to users.
            for key in ("rec_texts", "texts", "text_lines", "recognized_texts"):
                if key in value:
                    output.extend(cls._collect_text(value[key]))
            for key in ("rec_text", "text", "label"):
                item = value.get(key)
                if isinstance(item, str):
                    output.extend(cls._collect_text(item))
            if output:
                return output
            for item in value.values():
                output.extend(cls._collect_text(item))
            return output
        if isinstance(value, (list, tuple)):
            # Legacy PaddleOCR 2.x line result: [box, (text, confidence)]
            if len(value) == 2 and isinstance(value[1], (tuple, list)) and value[1] and isinstance(value[1][0], str):
                return cls._collect_text(value[1][0])
            for item in value:
                output.extend(cls._collect_text(item))
            return output
        if hasattr(value, "json"):
            try:
                json_value = value.json() if callable(value.json) else value.json
                return cls._collect_text(json_value)
            except Exception:
                pass
        if hasattr(value, "to_dict"):
            try:
                return cls._collect_text(value.to_dict())
            except Exception:
                pass
        return output

    def extract_text(self, image_path: Path) -> str:
        """Run local OCR and return non-empty recognised text."""

        engine = self._get_engine()
        try:
            if hasattr(engine, "predict"):
                result = list(engine.predict(str(image_path)))
            elif hasattr(engine, "ocr"):
                result = engine.ocr(str(image_path), cls=True)
            else:  # pragma: no cover - defensive unsupported package shape
                raise PaddleOcrRuntimeError(
                    "paddleocr_api_unsupported",
                    "The installed PaddleOCR version does not expose a supported local OCR method.",
                )
        except PaddleOcrRuntimeError:
            raise
        except Exception as exc:  # pragma: no cover - actual OCR runtime varies by host
            raise PaddleOcrRuntimeError(
                "paddleocr_processing_failed",
                "PaddleOCR could not read text from the scanned file.",
            ) from exc

        text = "\n".join(dict.fromkeys(part.strip() for part in self._collect_text(result) if part.strip())).strip()
        if not text:
            raise PaddleOcrRuntimeError(
                "ocr_no_text_detected",
                "OCR completed but no readable text was detected in the uploaded file.",
            )
        return text


paddle_ocr_adapter = PaddleOcrAdapter()
