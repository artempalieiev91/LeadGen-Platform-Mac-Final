"""
Еталонні пари «Title → Right Title» з CSV у репозиторії.
Файл: services/sheets_prep_data/title_training.csv
"""

from __future__ import annotations

import csv
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent / "sheets_prep_data"
_TITLE_TRAINING_CSV = _DATA_DIR / "title_training.csv"

COL_IN = "Title"
COL_OUT = "Right Title"

DEFAULT_MAX_CHARS = 52_000


def load_title_training_block(*, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """
    Few-shot для посад: кожен рядок «до → після».
    Якщо файл відсутній — порожній рядок.
    """
    if not _TITLE_TRAINING_CSV.is_file():
        return ""

    parts: list[str] = [
        "Еталонні пари з навчального файлу (як показувати посаду в розсилці). "
        "Для нових рядків застосовуй ту саму логіку (скорочення CEO/CTO/COO, уніфікація формулювань тощо):\n\n",
    ]
    total = sum(len(p) for p in parts)
    truncated = False

    with _TITLE_TRAINING_CSV.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return ""
        for row in reader:
            left = (row.get(COL_IN) or "").strip()
            right = (row.get(COL_OUT) or "").strip()
            line = f"- {left!r} → {right!r}\n"
            if total + len(line) > max_chars:
                truncated = True
                break
            parts.append(line)
            total += len(line)

    if truncated:
        parts.append(
            "\n… (частина прикладів пропущена через ліміт розміру промпту; "
            "повний набір у `services/sheets_prep_data/title_training.csv`.)\n"
        )

    return "".join(parts)
