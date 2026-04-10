"""
Підготовка CSV за логікою Google Apps Script (Sheets Preparation):
reorder → fillPersonLocation → updateIndustry → cleanDomains → (гіперпосилання лише в Sheets).
"""

from __future__ import annotations

import csv
import io
import re

from services.sheets_preparation_industry_map import INDUSTRY_MAPPING

# Порядок ключових стовпців як у reorderColumnsCustom(); решта колонок інпуту відкидаються.
COLUMN_ORDER: list[str] = [
    "Company Name for Emails",
    "Website",
    "Industry",
    "Company Country",
    "Company State",
    "Company City",
    "First Name",
    "Last Name",
    "Title",
    "Email",
    "Person Linkedin Url",
    "Company Linkedin Url",
    "# Employees",
    "Country",
    "State",
    "City",
]

APOLLO_CONTACT_ID_COLUMN = "Apollo Contact Id"
APOLLO_ACCOUNT_ID_COLUMN = "Apollo Account Id"

# Можливі назви колонки Apollo Contact у вхідному файлі (перше збігнення).
APOLLO_CONTACT_ID_INPUT_ALIASES: tuple[str, ...] = (
    "Apollo Contact Id",
    "Apollo Contact ID",
)

# Apollo Account Id у виході кроку 1 — з інпуту, в кінці таблиці (якщо колонка була у файлі).
APOLLO_ACCOUNT_ID_INPUT_ALIASES: tuple[str, ...] = (
    "Apollo Account Id",
    "Apollo Account ID",
)

# Повний заголовок результату кроку 1: ключові + Apollo Contact Id + Apollo Account Id.
OUTPUT_COLUMN_ORDER: list[str] = [
    *COLUMN_ORDER,
    APOLLO_CONTACT_ID_COLUMN,
    APOLLO_ACCOUNT_ID_COLUMN,
]


def _normalize_header_label(s: str) -> str:
    return " ".join((s or "").strip().split()).casefold()


def _header_index_map(header_row: list[str]) -> dict[str, int]:
    """Нормалізована назва → індекс (перше входження)."""
    m: dict[str, int] = {}
    for i, h in enumerate(header_row):
        key = _normalize_header_label(str(h))
        if key not in m:
            m[key] = i
    return m


def _column_index(norm_map: dict[str, int], canonical_name: str) -> int | None:
    k = _normalize_header_label(canonical_name)
    return norm_map.get(k)


def _resolve_apollo_contact_id_index(norm_map: dict[str, int]) -> int | None:
    for alias in APOLLO_CONTACT_ID_INPUT_ALIASES:
        idx = _column_index(norm_map, alias)
        if idx is not None:
            return idx
    return None


def _resolve_apollo_account_id_index(norm_map: dict[str, int]) -> int | None:
    for alias in APOLLO_ACCOUNT_ID_INPUT_ALIASES:
        idx = _column_index(norm_map, alias)
        if idx is not None:
            return idx
    return None


def _cell_at(full_row: list[str | None], row_width: int, col_idx: int | None) -> str:
    if col_idx is None or col_idx < 0:
        return ""
    padded = list(full_row) + [""] * max(0, row_width - len(full_row))
    if col_idx >= len(padded):
        return ""
    v = padded[col_idx]
    return "" if v is None else str(v)


def project_key_columns_and_apollo(rows: list[list[str]]) -> tuple[list[list[str]], str | None, list[str]]:
    """
    З широкого CSV залишає лише COLUMN_ORDER (відсутні ключі → порожні клітинки),
    додає в кінець Apollo Contact Id та Apollo Account Id з відповідних колонок інпуту (якщо були).
    """
    detail: list[str] = []
    if not rows:
        return [], "Файл порожній або без рядків.", detail

    header_row = rows[0]
    row_width = len(header_row)
    norm_map = _header_index_map(header_row)

    key_indices: list[int | None] = []
    missing: list[str] = []
    for name in COLUMN_ORDER:
        ix = _column_index(norm_map, name)
        key_indices.append(ix)
        if ix is None:
            missing.append(name)

    apollo_contact_ix = _resolve_apollo_contact_id_index(norm_map)
    if apollo_contact_ix is None:
        detail.append(
            "Колонку Apollo Contact Id у файлі не знайдено — у виході додано порожній стовпець «Apollo Contact Id»."
        )
    else:
        detail.append("Значення Apollo Contact Id взято з інпуту по кожному рядку.")

    apollo_account_ix = _resolve_apollo_account_id_index(norm_map)
    if apollo_account_ix is None:
        detail.append(
            "Колонку Apollo Account Id у файлі не знайдено — у виході додано порожній стовпець «Apollo Account Id»."
        )
    else:
        detail.append("Значення Apollo Account Id взято з інпуту по кожному рядку (стовпець у кінці таблиці).")

    if missing:
        detail.append(
            f"Не знайдено колонок (будуть порожні): {', '.join('«' + m + '»' for m in missing)}."
        )

    out_rows: list[list[str]] = []
    out_rows.append(OUTPUT_COLUMN_ORDER)

    for r in rows[1:]:
        padded = list(r) + [""] * max(0, row_width - len(r))
        key_vals = [_cell_at(padded, row_width, ix) for ix in key_indices]
        apollo_contact_val = _cell_at(padded, row_width, apollo_contact_ix)
        apollo_account_val = _cell_at(padded, row_width, apollo_account_ix)
        out_rows.append([*key_vals, apollo_contact_val, apollo_account_val])

    return out_rows, None, detail


def parse_csv_bytes(data: bytes) -> list[list[str]]:
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    f = io.StringIO(text)
    reader = csv.reader(f, dialect)
    return [list(r) for r in reader]


def rows_to_csv_bytes(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for row in rows:
        w.writerow(row)
    return buf.getvalue().encode("utf-8-sig")


def fill_person_location(rows: list[list[str]]) -> None:
    if len(rows) < 2:
        return
    headers = rows[0]

    def idx(name: str) -> int:
        return headers.index(name)

    try:
        company_country_i = idx("Company Country")
        company_state_i = idx("Company State")
        company_city_i = idx("Company City")
        person_country_i = idx("Country")
        person_state_i = idx("State")
        person_city_i = idx("City")
    except ValueError:
        return

    ncols = len(headers)
    for i in range(1, len(rows)):
        row = rows[i]
        while len(row) < ncols:
            row.append("")

        def get(ci: int) -> str:
            if 0 <= ci < len(row) and row[ci] is not None:
                return str(row[ci]).strip()
            return ""

        company_country = get(company_country_i)
        company_state = get(company_state_i)
        company_city = get(company_city_i)
        person_country = get(person_country_i)
        person_state = get(person_state_i)
        person_city = get(person_city_i)

        if (
            person_country
            and company_country
            and person_country.lower() == company_country.lower()
            and not person_state
            and not person_city
        ):
            row[person_state_i] = company_state
            row[person_city_i] = company_city
        elif (
            not person_city
            and person_country
            and person_state
            and company_country
            and company_state
            and person_country.lower() == company_country.lower()
            and person_state.lower() == company_state.lower()
        ):
            row[person_city_i] = company_city


def update_industry_column(rows: list[list[str]]) -> None:
    if len(rows) < 2:
        return
    headers = rows[0]
    if "Industry" not in headers:
        return
    industry_i = headers.index("Industry")
    for i in range(1, len(rows)):
        row = rows[i]
        if industry_i >= len(row):
            continue
        raw = row[industry_i]
        if raw is None or not str(raw).strip():
            continue
        key = str(raw).strip().lower()
        if key in INDUSTRY_MAPPING:
            row[industry_i] = INDUSTRY_MAPPING[key]


def remove_url_prefixes(url: str) -> str:
    s = "" if url is None else str(url)
    s = re.sub(r"^https?://", "", s, flags=re.I)
    s = re.sub(r"^www\.", "", s, flags=re.I)
    return s


def clean_domains(rows: list[list[str]]) -> None:
    if len(rows) < 2:
        return
    headers = rows[0]
    names = ("Website", "Person Linkedin Url", "Company Linkedin Url")
    indices = {n: headers.index(n) for n in names if n in headers}
    if not indices:
        return
    ncols = len(headers)
    for i in range(1, len(rows)):
        row = rows[i]
        while len(row) < ncols:
            row.append("")
        for _n, ci in indices.items():
            if ci < len(row) and row[ci]:
                row[ci] = remove_url_prefixes(str(row[ci]))


def run_sheets_preparation_pipeline(rows: list[list[str]]) -> tuple[list[list[str]], str | None, list[str]]:
    """
    Еквівалент automateProcess(): ключові стовпці + Apollo Contact Id + Apollo Account Id → fillPersonLocation → updateIndustry → cleanDomains.
    Крок removeUnwantedHyperlinks для плоского CSV не застосовується (лише формат Sheets).
    """
    log: list[str] = []
    if not rows:
        return [], "Файл порожній або без рядків.", log

    new_rows, err, project_detail = project_key_columns_and_apollo(rows)
    if err:
        return rows, err, log
    rows = new_rows
    log.append(
        "З широкого інпуту залишено лише ключові стовпці; у кінці — Apollo Contact Id та Apollo Account Id (з інпуту, якщо були колонки)."
    )
    log.extend(project_detail)

    fill_person_location(rows)
    log.append("Заповнено Country / State / City (правила fillPersonLocation).")

    update_industry_column(rows)
    log.append("Колонку Industry оновлено за INDUSTRY_MAPPING.")

    clean_domains(rows)
    log.append("Прибрано префікси http(s):// та www. у Website та LinkedIn URL.")

    log.append("removeUnwantedHyperlinks: для CSV пропущено (у Google Sheets знімає формат гіперпосилань).")

    return rows, None, log
