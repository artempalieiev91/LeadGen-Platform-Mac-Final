"""Збереження списку промптів Research Validation у JSON (локально в проєкті)."""

from __future__ import annotations

import json
from pathlib import Path

_PROMPTS_FILE = Path(__file__).resolve().parent.parent / "data" / "research_validation_prompts.json"


def prompts_path() -> Path:
    return _PROMPTS_FILE


def load_prompts() -> list[dict]:
    if not _PROMPTS_FILE.is_file():
        return []
    try:
        raw = json.loads(_PROMPTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    return [p for p in raw if isinstance(p, dict) and p.get("id") and p.get("text")]


def save_prompts(items: list[dict]) -> None:
    _PROMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PROMPTS_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
