"""
Крок 2 Sheets Preparation (AI): після «Company Name for Emails» → Right Company Name,
після «Title» → Right Title (OpenAI, формат для розсилок).
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable

from openai import OpenAI

from services.platform_openai import configure_openai_http_client
from services.sheets_preparation_pipeline import (
    _header_index_map,
    _normalize_header_label,
    parse_csv_bytes,
)
from services.sheets_preparation_company_format_rules import COMPANY_NAME_FORMAT_RULES_BLOCK
from services.sheets_preparation_step3_company_examples import (
    PROMPT_FEWSHOT_MAX_CHARS,
    load_company_name_training_block,
)
from services.sheets_preparation_step3_prompts import FALLBACK_COMPANY_HINT, FALLBACK_TITLE_HINT
from services.sheets_preparation_step3_title_examples import (
    TITLE_PROMPT_FEWSHOT_MAX_CHARS,
    load_title_training_block,
)
from services.sheets_preparation_title_retrieval import (
    EMBEDDING_MODEL,
    get_title_training_index,
    normalize_title_key,
)
from services.sheets_preparation_title_rules import TITLE_PRIORITY_PROMPT_BLOCK

AI_TIMEOUT_SEC = 120
BATCH_SIZE = 15

COL_COMPANY = "Company Name for Emails"
COL_TITLE = "Title"
COL_RIGHT_COMPANY = "Right Company Name"
COL_RIGHT_TITLE = "Right Title"


def _norm_h(s: str) -> str:
    return _normalize_header_label(s)


def _drop_columns_by_names(rows: list[list[str]], names: set[str]) -> list[list[str]]:
    if not rows:
        return rows
    nh = _normalize_header_label
    drop_idx = [i for i, h in enumerate(rows[0]) if nh(str(h)) in names]
    if not drop_idx:
        return rows
    drop_idx.sort(reverse=True)
    out: list[list[str]] = []
    for r in rows:
        nr = list(r)
        for di in drop_idx:
            if di < len(nr):
                nr.pop(di)
            elif len(nr) == di:
                pass
        out.append(nr)
    return out


def _insert_right_columns(header: list[str]) -> tuple[list[str], int | None, int | None]:
    """
    Після Company Name for Emails → Right Company Name; після Title → Right Title.
    Повертає новий заголовок і індекси нових колонок (для перевірки).
    """
    want_c = _norm_h(COL_COMPANY)
    want_t = _norm_h(COL_TITLE)
    has_c = any(_norm_h(str(h)) == want_c for h in header)
    has_t = any(_norm_h(str(h)) == want_t for h in header)
    if not has_c:
        raise ValueError(f"У CSV немає колонки «{COL_COMPANY}».")
    if not has_t:
        raise ValueError(f"У CSV немає колонки «{COL_TITLE}».")

    new_h: list[str] = []
    idx_rc: int | None = None
    idx_rt: int | None = None
    for h in header:
        new_h.append(h)
        hn = _norm_h(str(h))
        if hn == want_c:
            new_h.append(COL_RIGHT_COMPANY)
            idx_rc = len(new_h) - 1
        elif hn == want_t:
            new_h.append(COL_RIGHT_TITLE)
            idx_rt = len(new_h) - 1
    return new_h, idx_rc, idx_rt


def _build_row_aligned_to_header(header: list[str], row: list[str]) -> list[str]:
    w = len(header)
    r = list(row) + [""] * max(0, w - len(row))
    return r[:w]


def _chat_json(client: OpenAI, model: str, user_content: str) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": user_content}],
        timeout=float(AI_TIMEOUT_SEC),
        response_format={"type": "json_object"},
    )
    return (response.choices[0].message.content or "").strip()


def _parse_batch_response(raw: str, expected_indices: list[int]) -> dict[int, tuple[str, str]]:
    """Очікує JSON: {\"results\": [{\"i\": n, \"right_company\": \"...\", \"right_title\": \"...\"}, ...]}"""
    out: dict[int, tuple[str, str]] = {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            raise ValueError("Відповідь моделі не JSON.") from None
        data = json.loads(m.group(0))
    results = data.get("results")
    if not isinstance(results, list):
        raise ValueError("У JSON немає масиву results.")
    for item in results:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("i", -1))
        except (TypeError, ValueError):
            continue
        rc = str(item.get("right_company", "") or "").strip()
        rt = str(item.get("right_title", "") or "").strip()
        out[i] = (rc, rt)
    for exp in expected_indices:
        if exp not in out:
            out[exp] = ("", "")
    return out


def _company_name_dedupe_key(raw: str) -> str:
    """Ключ групування для однакового «Company Name for Emails» (відрізняються лише пробіли)."""
    return " ".join((raw or "").split())


def _canonicalize_right_company_by_input_company(
    pairs: list[tuple[int, str, str]],
    filled: dict[int, tuple[str, str]],
) -> int:
    """
    Для однакового значення Company Name for Emails виставляє один Right Company Name на всі рядки групи.
    Канон: найчастіше непорожнє значення серед відповідей AI; при рівності частот — перше у порядку файлу.
    Повертає кількість груп, де було більше одного різного непорожнього right_company (для журналу).
    """
    key_to_indices: dict[str, list[int]] = defaultdict(list)
    for i, comp, _tit in pairs:
        key_to_indices[_company_name_dedupe_key(comp)].append(i)

    merged_groups = 0
    for indices in key_to_indices.values():
        row_order_rc = [str(filled.get(idx, ("", ""))[0] or "").strip() for idx in indices]
        non_empty = [rc for rc in row_order_rc if rc]
        if not non_empty:
            canonical = ""
        else:
            cnt = Counter(non_empty)
            best = max(cnt.values())
            canonical = ""
            for rc in row_order_rc:
                if rc and cnt[rc] == best:
                    canonical = rc
                    break
            if len({rc for rc in non_empty}) > 1:
                merged_groups += 1

        for idx in indices:
            rt = filled.get(idx, ("", ""))[1]
            filled[idx] = (canonical, rt)

    return merged_groups


def _batch_prompt(
    batch: list[tuple[int, str, str]],
    company_shot: str,
    title_shot: str,
) -> str:
    lines = []
    for i, comp, tit in batch:
        lines.append(f'{i}\t{repr(comp)}\t{repr(tit)}')
    return f"""Ти допомагаєш підготувати дані для B2B розсилок. Потрібно відформатувати назви компаній та посади: читабельно, коректний регістр, без зайвих юридичних хвостів у назвах компаній (Inc., LLC тощо — прибирай, якщо це не частина бренду), посади — стандартний діловий стиль.

Якщо в батчі кілька рядків мають однакову назву компанії (той самий рядок після repr) — поле right_company для усіх цих рядків має бути однаковим рядком (однакове написання та регістр).

{company_shot}

{title_shot}

Вхід: рядки у форматі «індекс \\t назва_компанії \\t посада» (рядки нижче).
Вихід: один JSON-об'єкт з ключем "results" — масив об'єктів з полями:
- "i" — той самий індекс рядка (ціле число)
- "right_company" — відформатована назва компанії (рядок)
- "right_title" — відформатована посада (рядок); якщо немає впевненого канону з еталонів — порожній рядок (не вигадуй посаду).

Якщо вхід порожній — поверни порожній рядок у відповідному полі.

Рядки:
{chr(10).join(lines)}
"""


def run_step3_ai_format(
    rows: list[list[str]],
    *,
    api_key: str,
    model: str,
    company_few_shot: str | None = None,
    title_few_shot: str | None = None,
    on_progress: Callable[[float, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[list[list[str]], list[str], bool]:
    """
    Додає колонки Right Company Name та Right Title; заповнює через OpenAI батчами.
    on_progress(0..1, текст) — опційно для UI (прогрес-бар).
    should_stop — кооперативна зупинка між батчами (поточний HTTP-запит до OpenAI дораховується).
    Повертає (рядки, журнал, зупинено_користувачем).
    """
    log: list[str] = []
    if not rows or len(rows) < 2:
        raise ValueError("Порожній CSV.")

    nh = _norm_h
    drop_names = {nh(COL_RIGHT_COMPANY), nh(COL_RIGHT_TITLE)}
    working = _drop_columns_by_names([list(r) for r in rows], drop_names)

    header = working[0]
    new_header, _idx_rc, _idx_rt = _insert_right_columns(header)
    if _idx_rc is None or _idx_rt is None:
        raise ValueError("Не вдалося вставити службові колонки Right Company Name / Right Title.")

    nm = _header_index_map(header)
    ic = nm.get(_norm_h(COL_COMPANY))
    it = nm.get(_norm_h(COL_TITLE))
    if ic is None or it is None:
        raise ValueError("Внутрішня помилка: індекси Company / Title.")

    cw = len(header)
    data_rows: list[list[str]] = []
    pairs: list[tuple[int, str, str]] = []
    for ri in range(1, len(working)):
        row = working[ri]
        pr = list(row) + [""] * max(0, cw - len(row))
        pr = pr[:cw]
        comp = str(pr[ic] if ic < len(pr) else "").strip()
        tit = str(pr[it] if it < len(pr) else "").strip()
        pairs.append((ri - 1, comp, tit))
        data_rows.append(pr)

    if on_progress:
        on_progress(
            0.0,
            f"Зчитано **{len(pairs)}** рядків даних. Підготовка промпта (правила + фрагмент еталонів)…",
        )

    if company_few_shot is not None:
        cshot = company_few_shot.strip()
        company_rules_note = "передано параметром"
    else:
        base_rules = COMPANY_NAME_FORMAT_RULES_BLOCK.strip()
        file_blk = load_company_name_training_block(max_chars=PROMPT_FEWSHOT_MAX_CHARS)
        ex = file_blk.strip() if file_blk else ""
        if ex:
            cshot = (
                base_rules
                + "\n\nДодаткові еталонні пари з `company_name_training.csv` "
                f"(фрагмент, до ~{PROMPT_FEWSHOT_MAX_CHARS} символів; при конфліктах пріоритет у цих парах):\n\n"
                + ex
            )
            company_rules_note = (
                "`sheets_preparation_company_format_rules.py` + фрагмент "
                f"`company_name_training.csv` (≤{PROMPT_FEWSHOT_MAX_CHARS} симв.)"
            )
        else:
            cshot = base_rules + "\n\n" + FALLBACK_COMPANY_HINT.strip()
            company_rules_note = (
                "`sheets_preparation_company_format_rules.py` + резерв FALLBACK (CSV еталонів немає або порожній)"
            )

    title_priority_tail = "\n\n" + TITLE_PRIORITY_PROMPT_BLOCK.strip()
    title_index = get_title_training_index()

    if title_few_shot is not None:
        tshot_fixed = (title_few_shot.strip() + title_priority_tail).strip()
        title_strategy = "fixed"
        title_rules_note = "передано параметром + пріоритети title_rules"
    elif title_index.is_empty:
        tshot_fixed = (FALLBACK_TITLE_HINT.strip() + title_priority_tail).strip()
        title_strategy = "fixed"
        title_rules_note = "резерв FALLBACK (немає `title_training.csv`) + title_rules"
    else:
        tshot_fixed = None
        title_strategy = "retrieval"
        title_rules_note = (
            f"`title_training.csv`: embeddings **{EMBEDDING_MODEL}** (підбір еталонів на батч) "
            "+ exact lookup + title_rules"
        )

    total_rows = len(pairs)
    total_batches = (total_rows + BATCH_SIZE - 1) // BATCH_SIZE if total_rows else 0
    if on_progress:
        on_progress(
            0.0,
            f"Рядків для AI: **{total_rows}** · запитів до API: **{total_batches}** (до **{BATCH_SIZE}** рядків на запит). "
            "Далі — послідовні виклики OpenAI.",
        )

    client = OpenAI(api_key=api_key)
    configure_openai_http_client(client)
    filled: dict[int, tuple[str, str]] = {}
    stopped_by_user = False

    if title_strategy == "retrieval":
        try:
            title_index.embed_corpus(client)
        except Exception as exc:
            log.append(
                f"Embeddings для title_training недоступні ({exc!r}); "
                f"використано фрагмент CSV (до {TITLE_PROMPT_FEWSHOT_MAX_CHARS} симв.)."
            )
            file_blk_t = load_title_training_block(max_chars=TITLE_PROMPT_FEWSHOT_MAX_CHARS)
            blob = (file_blk_t.strip() if file_blk_t else "") or FALLBACK_TITLE_HINT.strip()
            tshot_fixed = (blob + title_priority_tail).strip()
            title_strategy = "fixed"
            title_rules_note = (
                f"fallback: фрагмент `title_training.csv` (≤{TITLE_PROMPT_FEWSHOT_MAX_CHARS} симв.) "
                "після помилки embeddings + title_rules"
            )

    for batch_num, start in enumerate(range(0, len(pairs), BATCH_SIZE), start=1):
        if should_stop and should_stop():
            stopped_by_user = True
            log.append(
                "Зупинка: подальші рядки залишено без AI — стовпці Right Company Name / Right Title порожні."
            )
            if on_progress:
                on_progress(
                    min(start / total_rows if total_rows else 1.0, 1.0),
                    "**Зупинка** — збирання часткового CSV…",
                )
            break
        chunk = pairs[start : start + BATCH_SIZE]
        if not chunk:
            continue
        indices = [c[0] for c in chunk]
        row_from = indices[0] + 1
        row_to = indices[-1] + 1
        if on_progress:
            done_before = start
            frac_before = done_before / total_rows if total_rows else 0.0
            on_progress(
                frac_before,
                f"Запит **{batch_num}** / **{total_batches}**: рядки **{row_from}–{row_to}** з **{total_rows}** (очікування відповіді OpenAI)…",
            )
        if title_strategy == "retrieval":
            titles_batch = [c[2] for c in chunk]
            piece = title_index.build_few_shot_for_queries(client, titles_batch).strip()
            if not piece:
                piece = (
                    "Немає непорожніх тайтлів у батчі для семантичного підбору еталонів; "
                    "орієнтуйся на правила нижче.\n"
                )
            tshot = (piece + title_priority_tail).strip()
        else:
            tshot = tshot_fixed or (FALLBACK_TITLE_HINT.strip() + title_priority_tail).strip()

        prompt = _batch_prompt(chunk, cshot, tshot)
        raw = _chat_json(client, model, prompt)
        batch_map = _parse_batch_response(raw, indices)
        filled.update(batch_map)
        if not title_index.is_empty:
            row_in_chunk = {c[0]: c for c in chunk}
            for idx in indices:
                c = row_in_chunk.get(idx)
                if c is None:
                    continue
                tit = c[2]
                key = normalize_title_key(tit)
                if key in title_index.exact_map:
                    rc, _rt = filled.get(idx, ("", ""))
                    filled[idx] = (rc, title_index.exact_map[key])
        log.append(
            f"AI батч рядків {indices[0] + 1}–{indices[-1] + 1}: отримано {len(batch_map)} записів."
        )
        if on_progress:
            done_after = min(start + len(chunk), total_rows)
            frac_after = done_after / total_rows if total_rows else 1.0
            on_progress(
                frac_after,
                f"Запит **{batch_num}** / **{total_batches}** завершено: оброблено **{done_after}** / **{total_rows}** рядків.",
            )

    merged = _canonicalize_right_company_by_input_company(pairs, filled)
    if merged:
        log.append(
            f"Узгоджено Right Company Name для **{merged}** груп з однаковим Company Name for Emails "
            "(один канонічний варіант на групу після відповідей AI)."
        )

    if on_progress:
        on_progress(1.0, "Збирання CSV з колонками Right Company Name та Right Title…")

    out_rows: list[list[str]] = [new_header]
    wn = len(new_header)
    for i, pr in enumerate(data_rows):
        base = _build_row_aligned_to_header(header, pr)
        new_r = []
        for j, col in enumerate(header):
            new_r.append(base[j] if j < len(base) else "")
            hn = _norm_h(str(col))
            if hn == _norm_h(COL_COMPANY):
                rc, rt = filled.get(i, ("", ""))
                new_r.append(rc)
            elif hn == _norm_h(COL_TITLE):
                rc, rt = filled.get(i, ("", ""))
                new_r.append(rt)
        while len(new_r) < wn:
            new_r.append("")
        out_rows.append(new_r[:wn])

    summary = (
        f"Крок 2 (AI): додано «{COL_RIGHT_COMPANY}» та «{COL_RIGHT_TITLE}»; модель {model}; батч по {BATCH_SIZE} рядків. "
        f"Назви компаній: {company_rules_note}. Посади: {title_rules_note}."
    )
    if stopped_by_user:
        summary = "[Частковий результат після зупинки] " + summary
    log.insert(0, summary)
    return out_rows, log, stopped_by_user


def run_step3_from_csv_bytes(
    csv_bytes: bytes,
    *,
    api_key: str,
    model: str,
    on_progress: Callable[[float, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[list[list[str]], list[str], bool]:
    rows = parse_csv_bytes(csv_bytes)
    return run_step3_ai_format(
        rows,
        api_key=api_key,
        model=model,
        on_progress=on_progress,
        should_stop=should_stop,
    )
