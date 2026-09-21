"""JSON persistence for data/.

Every write is atomic (temp file + rename): a long pull checkpoints repeatedly,
and an interrupted write must never leave a truncated dump behind.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, payload: Any) -> None:
    _write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=1))


def write_secret_json(path: Path, payload: Any) -> None:
    write_json(path, payload)
    path.chmod(0o600)


def read_model[T: BaseModel](path: Path, model: type[T]) -> T | None:
    if not path.exists():
        return None
    return model.model_validate_json(path.read_text())


def write_model(path: Path, payload: BaseModel) -> None:
    _write_atomic(path, payload.model_dump_json(indent=1))


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)
