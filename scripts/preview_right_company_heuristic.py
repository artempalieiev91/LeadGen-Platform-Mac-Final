"""Попередній перегляд Company Name for Emails → Right Company Name.

1) Точна пара з services/sheets_prep_data/company_name_training.csv (як у кроці 2 AI).
2) Інакше — евристики з services/sheets_preparation_company_format_rules.py (+ кінцеве « Company»).

Повний збіг з OpenAI у Streamlit не гарантується. Скільки рядків узято з CSV vs евристика — лише в stderr після запуску.
"""

from __future__ import annotations

import csv
import re
import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TRAINING_CSV = _REPO_ROOT / "services/sheets_prep_data/company_name_training.csv"
_TRAIN_IN = "Company Name for Emails"
_TRAIN_OUT = "Right Company Name"

_SUFFIX_CHAIN = [
    r"\s+GmbH\s+&\s+Co\.\s+KG$",
    r"\s+GmbH\s+U\.\s+Co\.\s+KG$",
    r"\s+GmbH\s+&\s+Co\.KG$",
    r"\s+Spedition\s+GmbH\s+&\s+Co\.KG$",
    r"\s+Spedition\s+GmbH\s+&\s+Co\.\s+KG$",
    r"\s+GmbH\s+&\s+Co\.$",
    r"\s+Logistik\s+GmbH\s+&\s+Co\.\s+KG$",
    r"\s+GmbH$",
    r"\s+gGmbH$",
    r"\s+AG$",
    r"\s+A/S$",
    r"\s+IKS$",
    r"\s+AS$",
    r"\s+B\.V\.$",
    r"\s+B\.V$",
    r"\s+BV$",
    r"\s+nv$",
    r"\s+NV$",
    r"\s+Oy$",
    r"\s+SAS$",
    r"\s+S\.A\.$",
    r"\s+AB$",
    r"\s+LP$",
    r"\s+PLC$",
    r"\s+KGaA$",
    r"\s+LLC$",
    r"\s+Inc\.?$",
    r"\s+Ltd\.?$",
]


def load_training_exact() -> dict[str, str]:
    if not _TRAINING_CSV.is_file():
        return {}
    left_to_rights: dict[str, list[str]] = {}
    with _TRAINING_CSV.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            left = (row.get(_TRAIN_IN) or "").strip()
            right = (row.get(_TRAIN_OUT) or "").strip()
            if not left or not right:
                continue
            left_to_rights.setdefault(left, []).append(right)
    return {k: Counter(v).most_common(1)[0][0] for k, v in left_to_rights.items()}


def _strip_trailing_company(s: str) -> str:
    t = s.strip()
    while t.lower().endswith(" company"):
        t = t[: -len(" company")].rstrip()
    return t


def _strip_suffixes(s: str) -> str:
    t = s.strip()
    t = re.sub(r"\s+Ltd\.\s*'[^']*'\s*$", "", t, flags=re.IGNORECASE)
    changed = True
    while changed:
        changed = False
        for pat in _SUFFIX_CHAIN:
            n = re.sub(pat, "", t, flags=re.IGNORECASE)
            if n != t:
                t = n.strip()
                changed = True
    return t.strip()


def _replace_separators(s: str) -> str:
    s = s.replace("•", " ").replace("|", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _title_if_shouting(s: str) -> str:
    letters = [c for c in s if c.isalpha()]
    if len(letters) < 4 or not letters:
        return s
    if not all(c.isupper() for c in letters):
        return s
    if any(c.islower() for c in s):
        return s
    words = s.split()
    out: list[str] = []
    for w in words:
        if "." in w:
            out.append(w.lower().title())
        elif len(w) <= 4 and w.isalpha() and w.isupper():
            out.append(w)
        else:
            out.append(w.capitalize())
    return " ".join(out)


def _fix_known(s: str) -> str:
    low = s.lower()
    if low == "exporto":
        return "Exporto"
    if low == "adp":
        return "ADP"
    if low == "ell":
        return "ELL"
    if low == "eqi":
        return "EQI"
    if low == "glc":
        return "GLC"
    if low == "ysds":
        return "YSDS"
    if low == "ipak":
        return "IPAK"
    return s


def suggest_right_company_heuristic(raw: str) -> str:
    if not (raw or "").strip():
        return ""
    s = raw.strip()
    s = _replace_separators(s)
    s = _strip_suffixes(s)
    s = _strip_trailing_company(s)
    s = _fix_known(s)
    if re.search(r"\.[a-z]{2,}$", s, re.I) and " " not in s:
        s = re.sub(r"\.[a-z]{2,}$", "", s, flags=re.I)
    s = _title_if_shouting(s)
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    return s.strip()


def suggest_right_company(raw: str, training: dict[str, str]) -> tuple[str, str]:
    key = (raw or "").strip()
    if key in training:
        return training[key], "company_name_training.csv"
    return suggest_right_company_heuristic(raw), "евристика"


def main() -> None:
    inp = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if inp is None or not inp.is_file():
        print("Usage: preview_right_company_heuristic.py <companies.txt>", file=sys.stderr)
        sys.exit(1)
    training = load_training_exact()
    lines = [ln.strip() for ln in inp.read_text(encoding="utf-8").splitlines() if ln.strip()]
    seen: set[str] = set()
    unique: list[str] = []
    for L in lines:
        if L not in seen:
            seen.add(L)
            unique.append(L)
    out_path = inp.with_suffix(".right_company_preview.csv")
    n_train = 0
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Company Name for Emails", "Right Company Name (наближення)"])
        for L in unique:
            right, src = suggest_right_company(L, training)
            if src == "company_name_training.csv":
                n_train += 1
            w.writerow([L, right])
    print(
        f"Wrote {len(unique)} rows → {out_path} "
        f"(еталон з company_name_training.csv: {n_train}, евристика: {len(unique) - n_train})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
