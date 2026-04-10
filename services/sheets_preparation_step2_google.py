"""
Крок 2 Sheets Preparation: завантажити Google Sheet (експорт CSV) і додати стовпець Apollo Account Id
у кінець файлу з буфера: спочатку зіставлення за Website, для решти — за Company Linkedin Url.
"""

from __future__ import annotations

import re
from typing import Iterable

import requests

from services.sheets_preparation_pipeline import (
    _column_index,
    _header_index_map,
    _normalize_header_label,
    parse_csv_bytes,
    remove_url_prefixes,
)

FETCH_TIMEOUT_SEC = 45

APOLLO_ACCOUNT_ID_COLUMN = "Apollo Account Id"

# Колонки в Google Таблиці
SHEET_WEB_ALIASES: tuple[str, ...] = (
    "Website",
    "Primary Website Url",
    "Company Website",
)
SHEET_LINKEDIN_ALIASES: tuple[str, ...] = (
    "Company Linkedin Url",
    "Company LinkedIn URL",
    "Company Linkedin",
)
SHEET_APOLLO_ACCOUNT_ALIASES: tuple[str, ...] = (
    "Apollo Account Id",
    "Apollo Account ID",
)


def parse_google_sheet_url(url: str) -> tuple[str, int]:
    u = (url or "").strip()
    if not u:
        raise ValueError("Посилання порожнє.")

    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", u)
    if not m:
        raise ValueError("Не вдалося знайти ID таблиці у посиланні (очікується docs.google.com/spreadsheets/d/...).")
    sheet_id = m.group(1)

    gid = 0
    gid_m = re.search(r"[#?&]gid=(\d+)", u)
    if gid_m:
        gid = int(gid_m.group(1))

    return sheet_id, gid


def google_sheet_export_csv_url(sheet_id: str, gid: int) -> str:
    return (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
        f"?format=csv&gid={gid}"
    )


def fetch_google_sheet_csv(url: str) -> bytes:
    sheet_id, gid = parse_google_sheet_url(url)
    export_url = google_sheet_export_csv_url(sheet_id, gid)
    r = requests.get(export_url, timeout=FETCH_TIMEOUT_SEC)
    if r.status_code == 403:
        raise RuntimeError(
            "Доступ заборонено (403). Відкрийте таблицю для перегляду за посиланням "
            "(Файл → Поділитися → «Будь-хто за посиланням» — переглядач) і повторіть."
        )
    if r.status_code == 404:
        raise RuntimeError("Таблицю не знайдено (404). Перевірте посилання та gid аркуша.")
    r.raise_for_status()
    return r.content


def _first_column_index(headers: list[str], aliases: Iterable[str]) -> int | None:
    nm = _header_index_map(headers)
    for a in aliases:
        k = _normalize_header_label(a)
        if k in nm:
            return nm[k]
    return None


def _norm_join_website(s: str) -> str:
    t = remove_url_prefixes(str(s or "").strip())
    t = t.lower().rstrip("/")
    if t.startswith("www."):
        t = t[4:]
    return t


def _norm_join_linkedin(s: str) -> str:
    return _norm_join_website(s)


def _pad_row(row: list[str], n: int) -> list[str]:
    r = list(row) + [""] * max(0, n - len(row))
    return r[:n]


def _drop_column_if_present(rows: list[list[str]], name: str) -> list[list[str]]:
    if not rows or name not in rows[0]:
        return rows
    idx = rows[0].index(name)
    out: list[list[str]] = []
    for r in rows:
        nr = list(r)
        while len(nr) <= idx:
            nr.append("")
        nr.pop(idx)
        out.append(nr)
    return out


def match_apollo_account_id_from_google_sheet(
    left_rows: list[list[str]],
    sheet_csv_bytes: bytes,
) -> tuple[list[list[str]], list[str]]:
    """
    Додає в кінець стовпець «Apollo Account Id» (якщо стовпець уже був — замінює).
    Для кожного рядка: спочатку збіг за Website у Google, інакше за Company Linkedin Url.
    Значення з колонки Apollo Account Id у Google Sheet (останній рядок виграшає при дублікаті ключа).
    """
    log: list[str] = []
    if not left_rows or len(left_rows) < 1:
        raise ValueError("Немає рядків для злиття (порожній CSV).")

    left_rows = _drop_column_if_present(list(left_rows), APOLLO_ACCOUNT_ID_COLUMN)

    right = parse_csv_bytes(sheet_csv_bytes)
    if not right:
        raise ValueError("Google Sheet порожній або не вдалося прочитати CSV експорту.")

    rh = right[0]
    r_width = len(rh)

    wi = _first_column_index(rh, SHEET_WEB_ALIASES)
    lii = _first_column_index(rh, SHEET_LINKEDIN_ALIASES)
    aai = _first_column_index(rh, SHEET_APOLLO_ACCOUNT_ALIASES)

    if aai is None:
        raise ValueError(
            "У Google Таблиці не знайдено колонку «Apollo Account Id» (або «Apollo Account ID»)."
        )
    if wi is None and lii is None:
        raise ValueError(
            "У Google Таблиці потрібні колонки Website та/або Company Linkedin Url для зіставлення."
        )

    # Нормалізований ключ -> останнє значення Apollo Account Id з таблиці
    web_to_aid: dict[str, str] = {}
    li_to_aid: dict[str, str] = {}

    for data_i in range(1, len(right)):
        row = _pad_row(right[data_i], r_width)
        aid_raw = str(row[aai]).strip() if aai < len(row) and row[aai] is not None else ""
        if not aid_raw:
            continue
        if wi is not None:
            wk = _norm_join_website(row[wi] if wi < len(row) else "")
            if wk:
                web_to_aid[wk] = aid_raw
        if lii is not None:
            lk = _norm_join_linkedin(row[lii] if lii < len(row) else "")
            if lk:
                li_to_aid[lk] = aid_raw

    log.append(
        "Зіставлення: для Website і LinkedIn з обох таблиць застосовується одна нормалізація — "
        "прибирається префікс http:// або https://, на початку прибирається www., "
        "рядок у нижньому регістрі, зрізається / в кінці. Тому `https://site.com/` і `site.com` дають той самий ключ."
    )
    log.append(
        f"Google: Website col={wi}, Company Linkedin col={lii}, Apollo Account Id col={aai}; "
        f"унікальних ключів Website→Id: {len(web_to_aid)}, LinkedIn→Id: {len(li_to_aid)}."
    )

    lh = left_rows[0]
    l_nm = _header_index_map(lh)
    l_website = _column_index(l_nm, "Website")
    l_cli = _column_index(l_nm, "Company Linkedin Url")

    out: list[list[str]] = []
    out.append([*lh, APOLLO_ACCOUNT_ID_COLUMN])

    by_web = 0
    by_li = 0
    empty = 0

    for li in range(1, len(left_rows)):
        lrow = _pad_row(left_rows[li], len(lh))
        aid_out = ""

        if l_website is not None and l_website < len(lrow):
            wk = _norm_join_website(lrow[l_website])
            if wk and wk in web_to_aid:
                aid_out = web_to_aid[wk]
                by_web += 1

        if not aid_out and l_cli is not None and l_cli < len(lrow):
            lk = _norm_join_linkedin(lrow[l_cli])
            if lk and lk in li_to_aid:
                aid_out = li_to_aid[lk]
                by_li += 1

        if not aid_out:
            empty += 1

        out.append([*lrow, aid_out])

    log.append(
        f"Заповнено Apollo Account Id: за Website — {by_web} рядків; додатково за LinkedIn (без збігу за сайтом) — {by_li}; "
        f"без збігу — {empty}."
    )

    return out, log
