from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AppError(Exception):
    status_code: int
    code: str
    message: str
    fields: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


def not_found(resource: str = "レシート") -> AppError:
    return AppError(404, "not_found", f"{resource}が見つかりません。")


def conflict(message: str, code: str = "conflict") -> AppError:
    return AppError(409, code, message)
