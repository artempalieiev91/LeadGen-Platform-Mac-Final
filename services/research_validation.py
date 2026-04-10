"""
Research Validation: крок 1 — рядок CSV без знімання сайту; крок 2 — логіка як у
https://github.com/artempalieiev91/OpenAI4omini-Python-Mac (fetch HTML + текст + OpenAI).
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup
from openai import OpenAI

from services.platform_openai import configure_openai_http_client

# Маркери узгоджені з оригінальним скриптом + додатковий для кроку 2
MARKER_RELEVANT = "Relevant123"
MARKER_NOT_RELEVANT = "Relevant234"
MARKER_NEED_SITE = "Step2Needed789"

# Колонки з вхідного CSV (заголовок першого рядка). У модель на крок 1 — лише Website + Short Description.
WEBSITE_COLUMN_NAME = "Website"
SHORT_DESCRIPTION_COLUMN_NAME = "Short Description"
# Можливі назви колонок у різних експортах Apollo (перевіряються по черзі)
LINKEDIN_COLUMN_ALIASES: tuple[str, ...] = (
    "Company Linkedin Url",
    "Company LinkedIn Url",
    "Company Linkedin URL",
    "Company LinkedIn URL",
)
APOLLO_ID_COLUMN_ALIASES: tuple[str, ...] = (
    "Apollo Account Id",
    "Apollo Account ID",
)

# Вихідний CSV — завжди 6 колонок у такому порядку (заголовки фіксовані)
OUTPUT_COL_LINKEDIN = "Company Linkedin Url"
OUTPUT_COL_APOLLO_ID = "Apollo Account Id"
OUTPUT_COL_WEBSITE = "Вебсайт"
OUTPUT_COL_RELEVANCE = "Релевантність"
OUTPUT_COL_DESCRIPTION = "Опис"
OUTPUT_COL_SOURCE = "Джерело рішення"

# Пояснення для колонки «Джерело рішення»
SOURCE_STEP1_CSV = "Крок 1 — лише дані з CSV (сайт не завантажувався)"
SOURCE_STEP2_WEB = "Крок 2 — текст з вебсайту"
SOURCE_NO_WEBSITE_CELL = "— (порожня колонка Website)"
SOURCE_STEP1_API_FAIL = "Крок 1 — помилка API"
SOURCE_STEP2_FETCH_FAIL = "Крок 2 — сторінку не отримано"
SOURCE_STEP2_API_FAIL = "Крок 2 — помилка API"
SOURCE_USER_STOPPED = "Обробку зупинено користувачем"

# Частковий результат після зупинки (рядки, що не встигли обробити)
RV_STOP_STATUS = "Перервано"
RV_STOP_DESCRIPTION = "Рядок не оброблено (зупинка)"

AI_TIMEOUT_SEC = 55
FETCH_TIMEOUT_SEC = 10
TEXT_MAX_LEN = 8000
USER_AGENT = "Mozilla/5.0 (compatible; Python-requests; research-validation)"

STEP1_MARKER_BLOCK = f"""
---
### Системні маркери відповіді (обов’язково збережіть їх у відповіді):
- Якщо компанія **релевантна** за критеріями з промпту: почніть з `{MARKER_RELEVANT}` (одним рядком або на початку), далі — короткий опис у форматі, який ви задали в промпті вище.
- Якщо **не релевантна**: відповідь має містити `{MARKER_NOT_RELEVANT}`.
- Якщо **неможливо вирішити** лише за даними рядка (потрібен перегляд сайту): відповідь має містити `{MARKER_NEED_SITE}` (без опису релевантності).
---
"""


@dataclass
class AiOutcome:
    status: str  # "relevant" | "not_relevant" | "need_site" | "unknown"
    description: str


def _clean_description(raw: str) -> str:
    s = raw
    for m in (MARKER_RELEVANT, MARKER_NOT_RELEVANT, MARKER_NEED_SITE):
        s = s.replace(m, "")
    return re.sub(r"\s+", " ", s).strip()


def parse_markers(response_str: str) -> AiOutcome:
    text = (response_str or "").strip()
    upper = text
    if MARKER_NEED_SITE in upper:
        return AiOutcome("need_site", _clean_description(text))
    if MARKER_RELEVANT in upper:
        return AiOutcome("relevant", _clean_description(text))
    if MARKER_NOT_RELEVANT in upper:
        return AiOutcome("not_relevant", _clean_description(text))
    # Немає чітких маркерів — вважаємо, що потрібен перегляд сайту
    return AiOutcome("need_site", text)


def fetch_site_text(url: str) -> str:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    variants: list[str] = []
    u = (url or "").strip()
    if re.match(r"^https?://", u, re.I):
        variants.append(u)
    else:
        variants.extend([f"http://{u}", f"https://{u}"])
    html = ""
    for variant in variants:
        try:
            r = session.get(variant, timeout=FETCH_TIMEOUT_SEC, allow_redirects=True)
            if 200 <= r.status_code < 300:
                html = r.text or ""
                break
        except requests.RequestException:
            continue
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    body = soup.body or soup
    text = body.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:TEXT_MAX_LEN] if len(text) > TEXT_MAX_LEN else text


def _chat(
    client: OpenAI,
    model: str,
    user_content: str,
) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": user_content}],
        timeout=float(AI_TIMEOUT_SEC),
    )
    return (response.choices[0].message.content or "").strip()


def run_step1_on_row(
    client: OpenAI,
    model: str,
    base_prompt: str,
    row_label: str,
    row_payload: str,
) -> AiOutcome:
    prompt = f"{base_prompt}{STEP1_MARKER_BLOCK}\n\n### Дані рядка ({row_label}):\n{row_payload}"
    raw = _chat(client, model, prompt)
    return parse_markers(raw)


def run_step2_on_site_text(
    client: OpenAI,
    model: str,
    base_prompt: str,
    page_text: str,
) -> AiOutcome:
    # Як у analyze_sites.py: prompt + "Contents: " + text
    user_content = f"{base_prompt}\n\nContents: {page_text}"
    raw = _chat(client, model, user_content)
    out = parse_markers(raw)
    # На крокі 2 сайт уже відкритий — need_site трактуємо як unknown
    if out.status == "need_site":
        return AiOutcome("unknown", out.description or raw)
    return out


def _parse_csv_rows(data: bytes) -> tuple[list[str], list[list[str]]]:
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    f = io.StringIO(text)
    reader = csv.reader(f, dialect)
    rows = [list(r) for r in reader if any((c or "").strip() for c in r)]
    if not rows:
        return [], []
    header = [h.strip() if h else f"col_{i}" for i, h in enumerate(rows[0])]
    data_rows = rows[1:] if len(rows) > 1 else rows
    # якщо один рядок і схоже на дані без заголовка — використати як дані
    if len(rows) == 1:
        header = [f"col_{i}" for i in range(len(rows[0]))]
        data_rows = rows
    return header, data_rows


def _normalize_header_label(s: str) -> str:
    return " ".join((s or "").strip().split()).casefold()


def _column_index_map(header: list[str]) -> dict[str, int]:
    """Нормалізована назва заголовка → індекс (перше входження). Для зіставлення з інпутом."""
    m: dict[str, int] = {}
    for i, h in enumerate(header):
        key = _normalize_header_label(h)
        if key not in m:
            m[key] = i
    return m


def _find_website_column_index(header: list[str]) -> int | None:
    # Website — без урахування регістру / пробілів
    t = _normalize_header_label(WEBSITE_COLUMN_NAME)
    for i, h in enumerate(header):
        if _normalize_header_label(h) == t:
            return i
    return None


def _find_short_description_column_index(header: list[str]) -> int | None:
    t = _normalize_header_label(SHORT_DESCRIPTION_COLUMN_NAME)
    for i, h in enumerate(header):
        if _normalize_header_label(h) == t:
            return i
    return None


def _row_to_url_and_short_description(
    header: list[str],
    cells: list[str],
    website_idx: int,
    short_desc_idx: int,
) -> tuple[str, str]:
    """Лише Website + Short Description потрапляють у крок 1 (решта колонок CSV ігнорується)."""
    padded = list(cells) + [""] * max(0, len(header) - len(cells))
    url = (padded[website_idx] if 0 <= website_idx < len(padded) else "").strip()
    sd = (padded[short_desc_idx] if 0 <= short_desc_idx < len(padded) else "").strip()
    if sd:
        rest = f"{SHORT_DESCRIPTION_COLUMN_NAME}: {sd}"
    else:
        rest = f"({SHORT_DESCRIPTION_COLUMN_NAME}: порожньо)"
    return url, rest


def _cell_value_at_index(header: list[str], cells: list[str], col_idx: int) -> str:
    padded = list(cells) + [""] * max(0, len(header) - len(cells))
    if not (0 <= col_idx < len(padded)):
        return ""
    return (padded[col_idx] or "").strip()


def _cell_by_normalized_names(
    header: list[str],
    cells: list[str],
    col_map: dict[str, int],
    aliases: tuple[str, ...],
) -> str:
    for alias in aliases:
        nk = _normalize_header_label(alias)
        if nk in col_map:
            return _cell_value_at_index(header, cells, col_map[nk])
    return ""


def _linkedin_from_input_row(header: list[str], cells: list[str], col_map: dict[str, int]) -> str:
    """Беремо з того ж рядка інпуту: спочатку відомі назви Apollo, потім евристика по заголовку."""
    v = _cell_by_normalized_names(header, cells, col_map, LINKEDIN_COLUMN_ALIASES)
    if v:
        return v
    for i, h in enumerate(header):
        hl = _normalize_header_label(h)
        if "linkedin" in hl and "url" in hl:
            return _cell_value_at_index(header, cells, i)
    return ""


def _apollo_from_input_row(header: list[str], cells: list[str], col_map: dict[str, int]) -> str:
    v = _cell_by_normalized_names(header, cells, col_map, APOLLO_ID_COLUMN_ALIASES)
    if v:
        return v
    for i, h in enumerate(header):
        hl = _normalize_header_label(h)
        if "apollo" in hl and "account" in hl:
            return _cell_value_at_index(header, cells, i)
    return ""


def _status_to_label(st: str) -> str:
    return {
        "relevant": "Релевантна",
        "not_relevant": "Не релевантна",
        "need_site": "Потрібен сайт",
        "unknown": "Невизначено",
    }.get(st, st)


def _relevance_for_csv_column(status_display: str) -> str:
    """Для вихідної колонки «Релевантність»: Так / Ні або службовий текст."""
    if status_display == _status_to_label("relevant"):
        return "Так"
    if status_display == _status_to_label("not_relevant"):
        return "Ні"
    return status_display


def _stopped_row_triple() -> tuple[str, str, str]:
    return (RV_STOP_STATUS, RV_STOP_DESCRIPTION, SOURCE_USER_STOPPED)


def _process_one_data_row_merged(
    client: OpenAI,
    model: str,
    user_prompt: str,
    header: list[str],
    cells: list[str],
    idx: int,
    n: int,
    website_idx: int,
    short_desc_idx: int,
    col_map: dict[str, int],
    log_lines: list[str],
    on_progress: Callable[[float, str], None] | None,
) -> tuple[str, str, str]:
    """Один рядок: крок 1 (CSV) і за потреби крок 2 (сайт) — як послідовні два проходи, але без проміжного маркера __STEP2__."""
    row_no = idx + 1

    def log(msg: str) -> None:
        log_lines.append(msg)

    if on_progress:
        on_progress((idx + 1) / max(n, 1), f"Рядок {row_no} / {n}")

    url, rest = _row_to_url_and_short_description(header, cells, website_idx, short_desc_idx)
    if not url:
        log(f"Рядок {row_no}: порожня колонка Website — крок 2 не застосовується")
        return ("Помилка", f"Порожня колонка «{WEBSITE_COLUMN_NAME}»", SOURCE_NO_WEBSITE_CELL)

    try:
        o1 = run_step1_on_row(
            client,
            model,
            user_prompt,
            f"рядок {row_no}",
            f"URL: {url}\n{rest}",
        )
    except Exception as e:
        log(f"Рядок {row_no} крок 1 API: {e}")
        return ("Помилка API", str(e)[:800], SOURCE_STEP1_API_FAIL)

    dsc_preview = (o1.description[:120] + "…") if len(o1.description) > 120 else o1.description
    log(f"Рядок {row_no} [крок 1]: {o1.status} — {dsc_preview}")

    if o1.status == "relevant":
        return (_status_to_label("relevant"), o1.description, SOURCE_STEP1_CSV)
    if o1.status == "not_relevant":
        return (_status_to_label("not_relevant"), o1.description, SOURCE_STEP1_CSV)

    log(f"Крок 2: завантаження {url}")
    page_text = fetch_site_text(url)
    if not page_text:
        log("  → порожній контент")
        return (
            "Не релевантна / немає тексту",
            "Не вдалося завантажити сторінку",
            SOURCE_STEP2_FETCH_FAIL,
        )
    try:
        o2 = run_step2_on_site_text(client, model, user_prompt, page_text)
    except Exception as e:
        log(f"  → API помилка: {e}")
        return ("Помилка API", str(e)[:800], SOURCE_STEP2_API_FAIL)
    log(f"  → {o2.status}")
    if o2.status == "relevant":
        return (_status_to_label("relevant"), o2.description, SOURCE_STEP2_WEB)
    if o2.status == "not_relevant":
        return (_status_to_label("not_relevant"), o2.description, SOURCE_STEP2_WEB)
    return (_status_to_label("unknown"), o2.description or "", SOURCE_STEP2_WEB)


def _results_to_csv_bytes(
    header: list[str],
    data_rows: list[list[str]],
    website_idx: int,
    col_map: dict[str, int],
    results: list[tuple[str, str, str]],
    log_lines: list[str],
) -> tuple[bytes, str]:
    out_lines: list[list[str]] = [
        [
            OUTPUT_COL_LINKEDIN,
            OUTPUT_COL_APOLLO_ID,
            OUTPUT_COL_WEBSITE,
            OUTPUT_COL_RELEVANCE,
            OUTPUT_COL_SOURCE,
            OUTPUT_COL_DESCRIPTION,
        ],
    ]
    if len(results) != len(data_rows):
        raise RuntimeError("Внутрішня помилка: кількість результатів не збігається з рядками CSV.")
    for cells, (st, dsc, source_note) in zip(data_rows, results):
        linkedin_out = _linkedin_from_input_row(header, cells, col_map)
        apollo_out = _apollo_from_input_row(header, cells, col_map)
        website_out = _cell_value_at_index(header, cells, website_idx)
        out_lines.append(
            [
                linkedin_out,
                apollo_out,
                website_out,
                _relevance_for_csv_column(st),
                source_note,
                dsc,
            ]
        )

    _expected_cols = 6
    for ri, row in enumerate(out_lines):
        if len(row) != _expected_cols:
            raise RuntimeError(
                f"Внутрішня помилка: рядок {ri} має {len(row)} колонок, очікується {_expected_cols}."
            )

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for row in out_lines:
        w.writerow(row)
    return buf.getvalue().encode("utf-8-sig"), "\n".join(log_lines)


def _run_merged_pipeline(
    client: OpenAI,
    model: str,
    user_prompt: str,
    header: list[str],
    data_rows: list[list[str]],
    on_progress: Callable[[float, str], None] | None,
    log_lines: list[str],
    stop_check: Callable[[], bool] | None,
) -> tuple[bytes, str]:
    n = len(data_rows)
    results: list[tuple[str, str, str]] = []

    website_idx = _find_website_column_index(header)
    short_desc_idx = _find_short_description_column_index(header)
    col_map = _column_index_map(header)
    if website_idx is None or short_desc_idx is None:
        raise ValueError(
            f'У першому рядку CSV обов’язково мають бути колонки «{WEBSITE_COLUMN_NAME}» та '
            f'«{SHORT_DESCRIPTION_COLUMN_NAME}». '
            "Колонки LinkedIn / Apollo за наявності копіюються у вихід; якщо їх немає — "
            "у виході будуть порожні клітинки, але заголовки перших двох колонок все одно присутні."
        )

    for idx, cells in enumerate(data_rows):
        if stop_check and stop_check():
            results.extend([_stopped_row_triple()] * (n - idx))
            break
        results.append(
            _process_one_data_row_merged(
                client,
                model,
                user_prompt,
                header,
                cells,
                idx,
                n,
                website_idx,
                short_desc_idx,
                col_map,
                log_lines,
                on_progress,
            )
        )

    if len(results) != n:
        raise RuntimeError("Внутрішня помилка: неповний список результатів після проходу.")
    if on_progress:
        on_progress(1.0, "Готово")

    return _results_to_csv_bytes(header, data_rows, website_idx, col_map, results, log_lines)


def research_validation_validate_and_init_state(csv_bytes: bytes) -> dict:
    """
    Парсить CSV і готує стан для покрокової обробки (Streamlit: кілька rerun між блоками рядків).
    """
    header, data_rows = _parse_csv_rows(csv_bytes)
    if not data_rows:
        raise ValueError("CSV порожній або нечитабельний.")
    website_idx = _find_website_column_index(header)
    short_desc_idx = _find_short_description_column_index(header)
    if website_idx is None or short_desc_idx is None:
        raise ValueError(
            f'У першому рядку CSV обов’язково мають бути колонки «{WEBSITE_COLUMN_NAME}» та '
            f'«{SHORT_DESCRIPTION_COLUMN_NAME}». '
            "Колонки LinkedIn / Apollo за наявності копіюються у вихід; якщо їх немає — "
            "у виході будуть порожні клітинки, але заголовки перших двох колонок все одно присутні."
        )
    col_map = _column_index_map(header)
    return {
        "header": header,
        "data_rows": data_rows,
        "website_idx": website_idx,
        "short_desc_idx": short_desc_idx,
        "col_map": col_map,
        "results": [],
        "next_idx": 0,
        "log_lines": [],
    }


def research_validation_state_step(
    state: dict,
    *,
    user_prompt: str,
    model: str,
    api_key: str,
    max_rows: int,
    on_progress: Callable[[float, str], None] | None = None,
) -> dict:
    """Обробляє до max_rows наступних рядків; оновлює state на місці й повертає його."""
    header = state["header"]
    data_rows = state["data_rows"]
    n = len(data_rows)
    client = OpenAI(api_key=api_key)
    configure_openai_http_client(client)
    website_idx = state["website_idx"]
    short_desc_idx = state["short_desc_idx"]
    col_map = state["col_map"]
    log_lines: list[str] = state["log_lines"]
    results: list[tuple[str, str, str]] = state["results"]

    processed = 0
    while processed < max_rows and state["next_idx"] < n:
        idx = state["next_idx"]
        triple = _process_one_data_row_merged(
            client,
            model,
            user_prompt,
            header,
            data_rows[idx],
            idx,
            n,
            website_idx,
            short_desc_idx,
            col_map,
            log_lines,
            on_progress,
        )
        results.append(triple)
        state["next_idx"] = idx + 1
        processed += 1
    return state


def research_validation_state_finalize(state: dict, *, user_stopped: bool) -> tuple[bytes, str]:
    """Формує CSV: якщо user_stopped — дописує службові рядки для необроблених позицій."""
    header = state["header"]
    data_rows = state["data_rows"]
    n = len(data_rows)
    results: list[tuple[str, str, str]] = list(state["results"])
    log_lines = state["log_lines"]
    website_idx = state["website_idx"]
    col_map = state["col_map"]

    if user_stopped and len(results) < n:
        log_lines.append(
            f"Зупинка: збережено {len(results)} з {n} рядків; решта позначені як «{RV_STOP_STATUS}»."
        )
        while len(results) < n:
            results.append(_stopped_row_triple())

    if len(results) != n:
        raise RuntimeError(
            f"Внутрішня помилка: очікувалось {n} результатів, є {len(results)}."
        )
    return _results_to_csv_bytes(header, data_rows, website_idx, col_map, results, log_lines)


def run_research_validation(
    csv_bytes: bytes,
    user_prompt: str,
    model: str,
    api_key: str,
    on_progress: Callable[[float, str], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> tuple[bytes, str]:
    """Повертає CSV: LinkedIn URL, Apollo Id, Вебсайт, Релевантність, Джерело рішення, Опис; плюс журнал."""
    log_lines: list[str] = []
    client = OpenAI(api_key=api_key)
    configure_openai_http_client(client)
    header, data_rows = _parse_csv_rows(csv_bytes)
    if not data_rows:
        raise ValueError("CSV порожній або нечитабельний.")
    return _run_merged_pipeline(
        client,
        model,
        user_prompt,
        header,
        data_rows,
        on_progress,
        log_lines,
        stop_check,
    )
