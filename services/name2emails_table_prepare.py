"""Підготовка CSV для Name2Email: колонка «Emails Research» одразу після Email (як формула в Excel)."""

from __future__ import annotations

import csv
import io
import re

EMAILS_RESEARCH_COL = "Emails Research"


def _normalize_header(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").strip().lower())


def _pick_exact(fieldnames: list[str], name: str) -> str | None:
    want = _normalize_header(name)
    for f in fieldnames:
        if _normalize_header(f) == want:
            return f
    return None


def _pick_email_column(fieldnames: list[str]) -> str | None:
    """Колонка Email / Emails — не плутати з «Emails Research»."""
    cands = (
        "email",
        "emails",
        "e-mail",
        "e_mail",
        "email address",
        "електронна пошта",
        "імейл",
        "пошта",
    )
    fn_orig = list(fieldnames)
    fn_norm = [_normalize_header(h) for h in fn_orig]
    for i, h in enumerate(fn_norm):
        if "research" in h:
            continue
        for cand in cands:
            cn = _normalize_header(cand)
            if h == cn:
                return fn_orig[i]
    for i, h in enumerate(fn_norm):
        if "research" in h:
            continue
        for cand in cands:
            cn = _normalize_header(cand)
            if len(cn) >= 4 and (cn in h or h.startswith(cn)):
                return fn_orig[i]
    return None


def _pick_column(fieldnames: list[str], candidates: tuple[str, ...]) -> str | None:
    fn_orig = list(fieldnames)
    fn_norm = [_normalize_header(h) for h in fn_orig]
    for cand in candidates:
        cn = _normalize_header(cand)
        for i, h in enumerate(fn_norm):
            if h == cn:
                return fn_orig[i]
    for cand in candidates:
        cn = _normalize_header(cand)
        if len(cn) < 3:
            continue
        for i, h in enumerate(fn_norm):
            if h == cn or (cn in h and len(cn) >= 4):
                return fn_orig[i]
    return None


def _normalize_website(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    s = re.sub(r"^https?://", "", s, flags=re.I)
    s = re.sub(r"^www\.", "", s, flags=re.I)
    s = s.split("/")[0].strip()
    return s


def build_emails_research_cell(first: str, last: str, website: str) -> str:
    """
    Еквівалент Excel: First Name & " " & Last Name & "@" & Website
    → «Ім'я Прізвище@домен» (без лапок у значенні).
    """
    first = (first or "").strip()
    last = (last or "").strip()
    w = _normalize_website(website)
    if not w:
        return ""
    name_part = f"{first} {last}".strip()
    if not name_part:
        return ""
    return f"{name_part}@{w}"


def prepare_emails_research_column(data: bytes) -> tuple[bytes, str | None]:
    """
    Додає стовпчик «Emails Research» одразу праворуч від колонки Email.
    У кожному рядку: First Name & " " & Last Name & "@" & Website.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return b"", "Файл не в UTF-8. Збережіть CSV як UTF-8 або UTF-8 з BOM."

    reader = csv.DictReader(io.StringIO(text))
    fn = reader.fieldnames
    if not fn:
        return b"", "У CSV немає заголовка або файл порожній."

    email_key = _pick_email_column(list(fn))
    first_key = _pick_column(
        list(fn),
        ("first name", "firstname", "first_name", "ім'я", "імя", "first"),
    )
    last_key = _pick_column(
        list(fn),
        ("last name", "lastname", "last_name", "прізвище", "last"),
    )
    site_key = _pick_column(
        list(fn),
        (
            "website",
            "web site",
            "company website",
            "company url",
            "domain",
            "site",
        ),
    )
    if not site_key:
        site_key = _pick_exact(list(fn), "url")

    if not email_key:
        return b"", "Не знайдено колонку Email (на кшталт «Email», «Emails», «E-mail»)."

    if not site_key:
        return b"", "Не знайдено колонку Website / домен."

    er_key = _pick_column(list(fn), ("emails research", "emails_research"))
    if not er_key:
        er_key = EMAILS_RESEARCH_COL

    rows = list(reader)
    if not rows:
        return b"", "Немає рядків даних."

    # Зберігаємо порядок колонок як у файлі; «Emails Research» — одразу після Email (не переносимо Email на початок).
    fn_list = list(fn)
    fn_no_er = [c for c in fn_list if c != er_key]
    try:
        email_idx = fn_no_er.index(email_key)
    except ValueError:
        return b"", "Внутрішня помилка: колонка Email зникла зі списку полів."
    out_fn = fn_no_er[: email_idx + 1] + [er_key] + fn_no_er[email_idx + 1 :]

    out_rows: list[dict[str, str]] = []
    for row in rows:
        first = (row.get(first_key) or "").strip() if first_key else ""
        last = (row.get(last_key) or "").strip() if last_key else ""
        site = (row.get(site_key) or "").strip() if site_key else ""
        cell = build_emails_research_cell(first, last, site)

        out_row: dict[str, str] = {}
        for k in out_fn:
            if k == email_key:
                v = row.get(email_key)
                out_row[k] = "" if v is None else str(v).strip()
            elif k == er_key:
                out_row[k] = cell
            else:
                v = row.get(k)
                out_row[k] = "" if v is None else str(v)

        out_rows.append(out_row)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=out_fn, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(out_rows)

    return buf.getvalue().encode("utf-8-sig"), None
