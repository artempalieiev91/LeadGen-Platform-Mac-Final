"""
Крок 4 Sheets Preparation: для рядків із «Видалено (…dead…)» / «Видалено (…No Match…)»
очищаються колонки Email і Domain (Results без змін).
"""

from __future__ import annotations

from services.sheets_prep_email_domain_gate import (
    COL_DOMAIN,
    COL_EMAIL,
    COL_RESULTS,
    results_that_clear_email_and_domain,
)
from services.sheets_preparation_pipeline import parse_csv_bytes, rows_to_csv_bytes


def _norm_h(s: str) -> str:
    return " ".join((s or "").strip().split()).casefold()


def _header_map(header: list[str]) -> dict[str, int]:
    m: dict[str, int] = {}
    for i, h in enumerate(header):
        k = _norm_h(str(h))
        if k not in m:
            m[k] = i
    return m


def _align_row(row: list[str], ncols: int) -> list[str]:
    r = list(row) + [""] * max(0, ncols - len(row))
    return r[:ncols]


def strip_bad_emails_from_csv_bytes(csv_bytes: bytes) -> tuple[bytes, str]:
    """
    Повертає (новий CSV, короткий журнал).
    Рядки з Results ∈ results_that_clear_email_and_domain() — Email і Domain стають порожніми.
    """
    rows = parse_csv_bytes(csv_bytes)
    if not rows or len(rows) < 2:
        return csv_bytes, "Немає даних для обробки."

    header = rows[0]
    hm = _header_map(header)
    ke, kd, kr = _norm_h(COL_EMAIL), _norm_h(COL_DOMAIN), _norm_h(COL_RESULTS)
    for label, key in (
        ("Email", ke),
        ("Domain", kd),
        ("Results", kr),
    ):
        if key not in hm:
            raise ValueError(f"У CSV немає колонки «{label}».")

    ei, di, ri = hm[ke], hm[kd], hm[kr]
    ncols = len(header)
    bad = results_that_clear_email_and_domain()
    cleared = 0

    out: list[list[str]] = [header]
    for row in rows[1:]:
        r = _align_row(row, ncols)
        res = (r[ri] if ri < len(r) else "").strip()
        if res in bad:
            if ei < len(r):
                r[ei] = ""
            if di < len(r):
                r[di] = ""
            # COL_RESULTS (ri) навмисно не змінюємо — залишаємо текст причини «Видалено».
            cleared += 1
        out.append(r)

    log = f"Очищено Email і Domain у рядках з «Видалено»: {cleared}."
    return rows_to_csv_bytes(out), log
