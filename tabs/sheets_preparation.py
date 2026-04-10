"""Вкладка Sheets Preparation — підготовка CSV за логікою Google Apps Script."""

from __future__ import annotations

import threading
import time
from datetime import datetime

import streamlit as st

from services.sheets_preparation_pipeline import (
    OUTPUT_COLUMN_ORDER,
    parse_csv_bytes,
    rows_to_csv_bytes,
    run_sheets_preparation_pipeline,
)
from services.platform_openai import openai_api_key_effective
from services.sheets_prep_email_domain_gate import (
    EmailDomainGateStopped,
    email_domain_gate_to_csv_bytes,
)
from services.sheets_preparation_step3_ai import run_step3_from_csv_bytes
from services.sheets_prep_strip_bad_emails import strip_bad_emails_from_csv_bytes
from services.telegram_notify import notify_task_finished

_PREVIEW_MAX_ROWS = 10


def _migrate_legacy_sheets_prep_session() -> None:
    """Одноразова сумісність: колишні ключі кроку 3 / Google кроку 2."""
    if st.session_state.get("sheets_prep_preview_after_step") == 3:
        st.session_state["sheets_prep_preview_after_step"] = 2
    pairs = (
        ("sheets_prep_step3_loaded_bytes", "sheets_prep_for_ai_bytes"),
        ("sheets_prep_step3_loaded_at", "sheets_prep_for_ai_at"),
    )
    for old, new in pairs:
        if old in st.session_state and new not in st.session_state:
            st.session_state[new] = st.session_state.pop(old)


def _unique_preview_headers(header: list[str]) -> list[str]:
    """Унікальні ключі для прев’ю (дублікати назв колонок ламають dict / dataframe)."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for h in header:
        base = str(h).strip() or "(без назви)"
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append(base if n == 0 else f"{base} ({n + 1})")
    return out


def _render_current_output_preview_and_download(
    *,
    dl_key: str,
    step_label: str = "",
    partial_note: str | None = None,
    data_bytes: bytes | None = None,
    log_text: str | None = None,
    expander_title: str = "Журнал (кроки 1–2)",
    download_label: str = "Завантажити поточний CSV",
    file_name: str = "sheets_preparation_output.csv",
    show_tg_caption: bool = True,
    tg_session_key: str | None = None,
    caption_after_success: str | None = None,
) -> None:
    """Прев’ю (до 10 рядків) та завантаження CSV. Якщо data_bytes не задано — береться sheets_preparation_out_bytes."""
    out = data_bytes if data_bytes is not None else st.session_state.get("sheets_preparation_out_bytes")
    if not out:
        return

    try:
        rows = parse_csv_bytes(out)
    except Exception:
        rows = []

    n_data = max(0, len(rows) - 1)
    title = "#### Поточний результат"
    if step_label:
        title += f" · {step_label}"
    st.markdown(title)
    if partial_note:
        st.warning(partial_note)
    st.success(
        f"Таблиця готова: **{n_data}** рядків даних · **{len(out):,}** байт CSV."
    )
    if caption_after_success:
        st.caption(caption_after_success)

    if len(rows) >= 2:
        header = rows[0]
        body = rows[1 : 1 + _PREVIEW_MAX_ROWS]
        keys = _unique_preview_headers(header)
        cols: dict[str, list[str]] = {k: [] for k in keys}
        for r in body:
            for i, k in enumerate(keys):
                cols[k].append(r[i] if i < len(r) else "")
        st.caption(
            f"Прев’ю перших **{len(body)}** рядків"
            + (" (є ще дані)" if n_data > len(body) else "")
            + "."
        )
        try:
            st.dataframe(cols, use_container_width=True, hide_index=True)
        except Exception as exc:
            st.warning(f"Не вдалося показати таблицю-прев’ю: {exc}. Файл усе одно можна завантажити нижче.")
    elif len(rows) == 1:
        st.caption("У файлі лише рядок заголовка, без рядків даних.")
    else:
        st.caption("Не вдалося показати прев’ю — файл порожній або некоректний.")

    if show_tg_caption:
        te = st.session_state.get(tg_session_key or "sheets_prep_tg_err")
        if te:
            st.caption(f"Telegram: {te}")

    st.download_button(
        download_label,
        data=out,
        file_name=file_name,
        mime="text/csv",
        key=dl_key,
    )
    _log_show = log_text if log_text is not None else (st.session_state.get("sheets_preparation_last_log") or "")
    with st.expander(expander_title, expanded=False):
        st.code(_log_show, language="text")


def _write_session_csv_copies(b: bytes) -> str:
    now = datetime.now().isoformat(timespec="seconds")
    st.session_state["sheets_prep_buffer_bytes"] = b
    st.session_state["sheets_prep_buffer_saved_at"] = now
    return now


def _autosave_after_step1() -> None:
    """Після кроку 1: буфер і CSV для кроку 2 (AI)."""
    out = st.session_state.get("sheets_preparation_out_bytes")
    if not out:
        return
    b = bytes(out)
    now_s = _write_session_csv_copies(b)
    st.session_state["sheets_prep_for_ai_bytes"] = b
    st.session_state["sheets_prep_for_ai_at"] = now_s


def _autosave_after_ai_step() -> None:
    """Після кроку 2 (AI): оновити буфер і джерело для повторного запуску."""
    out = st.session_state.get("sheets_preparation_out_bytes")
    if not out:
        return
    b = bytes(out)
    now_s = _write_session_csv_copies(b)
    st.session_state["sheets_prep_for_ai_bytes"] = b
    st.session_state["sheets_prep_for_ai_at"] = now_s


def _autosave_after_step3() -> None:
    """Після кроку 3: той самий буфер сесії, що після кроків 1–2 (`Підставити останній CSV знову`)."""
    out = st.session_state.get("sheets_prep_gate_out_bytes")
    if not out:
        return
    b = bytes(out)
    if not b:
        return
    now_s = _write_session_csv_copies(b)
    st.session_state["sheets_prep_for_ai_bytes"] = b
    st.session_state["sheets_prep_for_ai_at"] = now_s


def _autosave_after_step4() -> None:
    """Після кроку 4: буфер сесії та CSV для «Підставити останній CSV знову»."""
    out = st.session_state.get("sheets_prep_step4_out_bytes")
    if not out:
        return
    b = bytes(out)
    if not b:
        return
    now_s = _write_session_csv_copies(b)
    st.session_state["sheets_prep_for_ai_bytes"] = b
    st.session_state["sheets_prep_for_ai_at"] = now_s


def render_sheets_preparation() -> None:
    st.subheader("Sheets Preparation")
    _migrate_legacy_sheets_prep_session()

    st.markdown(
        """
Завантажте **CSV** з **будь-якою кількістю стовпців**: з інпуту беруться лише **ключові поля**. У **кінець** таблиці переносяться **Apollo Contact Id** та **Apollo Account Id** 
(якщо відповідні колонки вже є у файлі — значення зберігаються по рядках). Далі — як у `automateProcess`: локація людини → Industry → URL.

**Крок 2:** після кроку 1 через **OpenAI** додаються **Right Company Name** та **Right Title**. Ключ API — **на головній сторінці** (блок OpenAI) або в **Secrets** (`openai_api_key`). Під час роботи можна **Зупинити зі збереженням** — у файлі лишаються вже оброблені рядки.

**Крок 3 (опційно):** після кроку 2 — перевірка **Website** проти **домена з email** (логіка MathcURLs), колонки **Domain** та **Results** (Залишено / Видалено зі статусом MathcURLs); усі рядки лишаються у файлі. Після кроку 3 CSV **зберігається в буфер сесії** — та сама кнопка **«Підставити останній CSV знову»**, що й у кроках 2 і 4, підставляє **останній** файл з буфера у вхід поточного кроку.

**Крок 4 (опційно):** для таких рядків очищаються лише **Email** і **Domain**; колонка **Results** **не** змінюється — у ній лишається повний текст «Видалено (…)» з причиною з MathcURLs. Після кроку 4 CSV **зберігається в буфер**; під кроком 4 знову **«Підставити останній CSV знову»** для наступної дії.

Крок **removeUnwantedHyperlinks** у CSV недоступний (лише в Google Sheets).
        """
    )

    with st.expander("Вихідні колонки кроку 1 (порядок у результаті)", expanded=False):
        st.markdown("\n".join(f"{i + 1}. `{name}`" for i, name in enumerate(OUTPUT_COLUMN_ORDER)))

    st.markdown("### Крок 1: Підготовка CSV")
    st.caption(
        "Завантажте файл і натисніть **Запустити підготовку**. Після кроку 1 з’явиться **прев’ю перших 10 рядків** і **завантаження CSV**; нижче — **крок 2 (AI)**."
    )

    uploaded = st.file_uploader(
        "Вхідний CSV",
        type=["csv"],
        key="sheets_preparation_uploader",
        help="Багато стовпців OK. У кінці кроку 1: Apollo Contact Id та Apollo Account Id (якщо були в інпуті).",
    )

    if uploaded is not None:
        data = uploaded.getvalue()
        name = uploaded.name
        prev_bytes = st.session_state.get("sheets_preparation_data")
        prev_name = st.session_state.get("sheets_preparation_name")
        if prev_bytes != data or prev_name != name:
            st.session_state["sheets_preparation_data"] = data
            st.session_state["sheets_preparation_name"] = name
            st.session_state.pop("sheets_preparation_out_bytes", None)
            st.session_state.pop("sheets_preparation_last_log", None)
            st.session_state.pop("sheets_prep_tg_err", None)
            st.session_state.pop("sheets_prep_preview_after_step", None)
            st.session_state.pop("sheets_prep_for_ai_bytes", None)
            st.session_state.pop("sheets_prep_for_ai_at", None)
            st.session_state.pop("sheets_prep_step1_out_bytes", None)
            st.session_state.pop("sheets_prep_step1_last_log", None)
            st.session_state.pop("sheets_prep_gate_out_bytes", None)
            st.session_state.pop("sheets_prep_gate_log", None)
            st.session_state.pop("sheets_prep_step4_out_bytes", None)
            st.session_state.pop("sheets_prep_step4_log", None)
        st.caption(f"**{name}** · {uploaded.size:,} байт")
    elif st.session_state.get("sheets_preparation_data"):
        name = st.session_state.get("sheets_preparation_name") or "файл"
        st.caption(f"**{name}** · дані з попереднього завантаження в сесії")
    else:
        st.info("Завантажте CSV — дані зберігаються в сесії після вибору файлу.")

    # Після натискання іншої кнопки file_uploader часто повертає None — покладаємось на session_state;
    # bool(b"") дає False, тому перевіряємо len.
    _stored = st.session_state.get("sheets_preparation_data")
    has_input = uploaded is not None or (_stored is not None and len(_stored) > 0)

    if "sheets_prep_run" not in st.session_state:
        st.session_state["sheets_prep_run"] = False

    if has_input:
        if not st.session_state["sheets_prep_run"]:
            if st.button("Запустити підготовку", type="primary", key="sheets_prep_btn"):
                st.session_state["sheets_prep_run"] = True
                st.rerun()

    # Виконувати обробку поза «if has_input»: інакше після rerun без файлу у віджеті has_input міг стати False і пайплайн не запускався.
    if st.session_state["sheets_prep_run"]:
        st.session_state["sheets_prep_run"] = False
        raw = st.session_state.get("sheets_preparation_data") or b""
        if len(raw) == 0:
            st.error("Немає даних CSV у сесії. Завантажте файл ще раз і натисніть «Запустити підготовку».")
        else:
            try:
                rows = parse_csv_bytes(raw)
            except Exception as exc:
                st.error(f"Не вдалося прочитати CSV: {exc}")
            else:
                out_rows, err, log_lines = run_sheets_preparation_pipeline(rows)
                log_text = "\n".join(log_lines)

                if err:
                    st.error(err)
                    st.code(log_text or "(немає журналу)", language="text")
                else:
                    out_bytes = rows_to_csv_bytes(out_rows)
                    st.session_state["sheets_preparation_out_bytes"] = out_bytes
                    st.session_state["sheets_preparation_last_log"] = log_text
                    st.session_state["sheets_prep_step1_out_bytes"] = bytes(out_bytes)
                    st.session_state["sheets_prep_step1_last_log"] = log_text
                    _autosave_after_step1()
                    st.session_state["sheets_prep_gate_input_at"] = st.session_state.get(
                        "sheets_prep_buffer_saved_at"
                    )
                    st.session_state["sheets_prep_preview_after_step"] = 1
                    tg_err = notify_task_finished("Sheets Preparation — крок 1")
                    st.session_state["sheets_prep_tg_err"] = tg_err
                    st.rerun()

    if st.session_state.get("sheets_prep_step1_out_bytes"):
        st.divider()
        _render_current_output_preview_and_download(
            dl_key="sheets_prep_dl_step1",
            step_label="після кроку 1",
            data_bytes=st.session_state["sheets_prep_step1_out_bytes"],
            log_text=st.session_state.get("sheets_prep_step1_last_log") or "",
            expander_title="Журнал (крок 1)",
            download_label="Завантажити CSV після кроку 1",
            file_name="sheets_preparation_after_step1.csv",
            show_tg_caption=False,
        )

    st.divider()
    st.markdown("### Крок 2: Right Company Name та Right Title (AI)")
    st.caption(
        "Джерело — CSV після **кроку 1** (підставляється автоматично). Після запуску колонки "
        "**Right Company Name** та **Right Title** з’являться одразу після **Company Name for Emails** та **Title**. "
        "Під час роботи — **прогрес** і кнопка **Зупинити зі збереженням** (зупинка між батчами OpenAI; поточний запит дораховується). "
        "Після зупинки можна **завантажити частковий CSV**. "
        "Правила назв компаній: `services/sheets_preparation_company_format_rules.py` + фрагмент "
        "`company_name_training.csv`. Посади: embeddings + exact lookup по `title_training.csv` "
        "(модель `text-embedding-3-small`), інакше фрагмент файлу. Резерв — `step3_prompts.py`."
    )

    buf_for_ai = st.session_state.get("sheets_prep_buffer_bytes")
    c1, c2 = st.columns(2)
    with c1:
        if st.button(
            "Підставити останній CSV знову",
            key="sheets_prep_for_ai_reload",
            help="Підставити останній збережений CSV (після кроку 1, 2, 3 або 4).",
            disabled=buf_for_ai is None,
            use_container_width=True,
        ):
            st.session_state["sheets_prep_for_ai_bytes"] = bytes(buf_for_ai)
            st.session_state["sheets_prep_for_ai_at"] = datetime.now().isoformat(
                timespec="seconds"
            )
            st.success("CSV для кроку 2 оновлено.")
    with c2:
        loaded_ai = st.session_state.get("sheets_prep_for_ai_bytes")
        if loaded_ai:
            ts_ai = st.session_state.get("sheets_prep_for_ai_at") or "—"
            st.caption(f"Для кроку 2: **{len(loaded_ai):,}** байт · `{ts_ai}`")

    st.session_state.setdefault("sheets_prep_openai_model", "gpt-4o-mini")
    st.text_input(
        "Модель OpenAI",
        key="sheets_prep_openai_model",
        placeholder="gpt-4o-mini",
    )

    st.session_state.setdefault("sheets_prep_ai_requested", False)
    st.session_state.setdefault("sheets_prep_ai_bg_running", False)
    ai_src = st.session_state.get("sheets_prep_for_ai_bytes")
    api_eff = openai_api_key_effective()
    ai_go_disabled = ai_src is None or not api_eff
    ai_busy = st.session_state.get("sheets_prep_ai_bg_running", False)

    if not st.session_state.get("sheets_prep_ai_requested", False) and not ai_busy:
        if st.button(
            "Запустити крок 2 (AI)",
            type="primary",
            key="sheets_prep_ai_btn",
            disabled=ai_go_disabled,
        ):
            st.session_state["sheets_prep_ai_requested"] = True
            st.rerun()

    if ai_go_disabled:
        if ai_src is None:
            st.caption(
                "Спочатку виконайте **крок 1** — тоді CSV для кроку 2 підставиться автоматично (або **Підставити останній CSV знову**)."
            )
        elif not api_eff:
            st.caption("Введіть **API key OpenAI** на головній сторінці (блок OpenAI) або додайте `openai_api_key` у Secrets.")

    if st.session_state.get("sheets_prep_ai_requested"):
        st.session_state["sheets_prep_ai_requested"] = False
        raw_ai = st.session_state.get("sheets_prep_for_ai_bytes")
        api_run = openai_api_key_effective()
        model_run = (st.session_state.get("sheets_prep_openai_model") or "gpt-4o-mini").strip()
        if not raw_ai:
            st.error("Немає CSV для кроку 2 — виконайте **крок 1** або **Підставити останній CSV знову**.")
        elif not api_run:
            st.error("Потрібен OpenAI API key.")
        else:
            stop_ev = threading.Event()
            holder: dict = {"p": 0.0, "msg": "Підготовка…", "finished": False}

            def worker() -> None:
                try:

                    def on_progress(frac: float, msg: str) -> None:
                        holder["p"] = float(frac)
                        holder["msg"] = msg

                    out_rows, log_lines, stopped = run_step3_from_csv_bytes(
                        raw_ai,
                        api_key=api_run,
                        model=model_run,
                        on_progress=on_progress,
                        should_stop=stop_ev.is_set,
                    )
                    holder["out_rows"] = out_rows
                    holder["log_lines"] = log_lines
                    holder["stopped"] = stopped
                except Exception as exc:
                    holder["error"] = str(exc)
                finally:
                    holder["finished"] = True

            th = threading.Thread(target=worker, daemon=True, name="sheets-prep-ai")
            th.start()
            st.session_state["sheets_prep_ai_bg_running"] = True
            st.session_state["sheets_prep_ai_thread"] = th
            st.session_state["sheets_prep_ai_stop_ev"] = stop_ev
            st.session_state["sheets_prep_ai_holder"] = holder
            st.rerun()

    if st.session_state.get("sheets_prep_ai_bg_running"):
        th: threading.Thread = st.session_state["sheets_prep_ai_thread"]
        holder = st.session_state["sheets_prep_ai_holder"]
        stop_ev: threading.Event = st.session_state["sheets_prep_ai_stop_ev"]

        if th.is_alive():
            p = float(holder.get("p") or 0.0)
            prog_ai = st.progress(min(max(p, 0.0), 1.0))
            lbl_ai = st.empty()
            lbl_ai.markdown(str(holder.get("msg") or ""))
            if st.button(
                "Зупинити зі збереженням",
                type="secondary",
                key="sheets_prep_ai_stop_btn",
                help="Сигнал зупинки між батчами OpenAI; поточний запит до API дораховується до кінця.",
            ):
                stop_ev.set()
                st.rerun()
            st.info(
                "**Крок 3 не прихований** — він уже нижче на цій же вкладці. **Прокрутіть сторінку вниз** після цього блоку: "
                "заголовок **«Крок 3: Website ↔ домен email»** і велика кнопка **«Підставити останній CSV знову»**. "
                "Після завершення кроку 2 кнопка **«Запустити крок 3»** стане активною, коли в сесії з’явиться CSV "
                "(автоматично після AI або через **Підставити…** / завантаження файлу)."
            )
        else:
            st.session_state["sheets_prep_ai_bg_running"] = False
            err = holder.get("error")
            if err:
                st.error(str(err))
            else:
                out_ai = holder.get("out_rows")
                log_ai = holder.get("log_lines") or []
                stopped = bool(holder.get("stopped"))
                if out_ai is not None:
                    old_log = (st.session_state.get("sheets_preparation_last_log") or "").strip()
                    step2_block = "\n".join(log_ai)
                    combined = (
                        f"{old_log}\n\n--- Крок 2 (Right Company / Right Title) ---\n{step2_block}"
                        if old_log
                        else f"--- Крок 2 (Right Company / Right Title) ---\n{step2_block}"
                    )
                    st.session_state["sheets_preparation_out_bytes"] = rows_to_csv_bytes(out_ai)
                    st.session_state["sheets_preparation_last_log"] = combined
                    _autosave_after_ai_step()
                    st.session_state["sheets_prep_gate_input_at"] = st.session_state.get(
                        "sheets_prep_buffer_saved_at"
                    )
                    st.session_state["sheets_prep_preview_after_step"] = 2
                    st.session_state["sheets_prep_ai_last_partial"] = stopped
                    if stopped:
                        tg_err = notify_task_finished("Sheets Preparation — крок 2 (зупинено)")
                    else:
                        tg_err = notify_task_finished("Sheets Preparation — крок 2")
                    st.session_state["sheets_prep_tg_err"] = tg_err
            st.session_state.pop("sheets_prep_ai_thread", None)
            st.session_state.pop("sheets_prep_ai_stop_ev", None)
            st.session_state.pop("sheets_prep_ai_holder", None)
            st.rerun()

    if (
        st.session_state.get("sheets_prep_preview_after_step") == 2
        and st.session_state.get("sheets_preparation_out_bytes")
    ):
        st.divider()
        _partial = st.session_state.pop("sheets_prep_ai_last_partial", False)
        _render_current_output_preview_and_download(
            dl_key="sheets_prep_dl_s2",
            step_label="після кроку 2",
            partial_note=(
                "Частковий результат після зупинки: колонки Right Company Name / Right Title заповнені лише для уже оброблених рядків."
                if _partial
                else None
            ),
        )

    st.divider()
    st.markdown("### Крок 3: Website ↔ домен email (MathcURLs)")
    st.markdown("**Крок 3 — підставити CSV з буфера:**")
    st.caption(
        "Як у кроку 2: натисніть велику кнопку нижче, потім **Запустити крок 3**. Буфер оновлюється після кожного кроку 1–4."
    )

    st.session_state.setdefault("sheets_prep_gate_requested", False)
    st.session_state.setdefault("sheets_prep_gate_bg_running", False)

    gate_busy = st.session_state.get("sheets_prep_gate_bg_running", False)
    buf_for_gate = st.session_state.get("sheets_prep_buffer_bytes")

    if st.button(
        "Підставити останній CSV знову",
        key="sheets_prep_gate_reload",
        help="Підставити останній збережений CSV (після кроку 1, 2, 3 або 4). Буфер з’являється вже після кроку 1.",
        use_container_width=True,
    ):
        if buf_for_gate is None:
            st.warning(
                "У буфері сесії ще немає CSV. Виконайте **крок 1** (або крок 2), або завантажте файл з диска нижче."
            )
        else:
            st.session_state["sheets_preparation_out_bytes"] = bytes(buf_for_gate)
            st.session_state["sheets_prep_gate_input_at"] = datetime.now().isoformat(
                timespec="seconds"
            )
            st.session_state["sheets_prep_preview_after_step"] = 2
            st.session_state.pop("sheets_prep_gate_out_bytes", None)
            st.session_state.pop("sheets_prep_gate_log", None)
            st.session_state.pop("sheets_prep_step4_out_bytes", None)
            st.session_state.pop("sheets_prep_step4_log", None)
            st.success("CSV для кроку 3 оновлено.")

    _gs = st.session_state.get("sheets_preparation_out_bytes")
    if _gs:
        _tsg = st.session_state.get("sheets_prep_gate_input_at") or "—"
        st.caption(f"Для кроку 3: **{len(_gs):,}** байт · `{_tsg}`")

    with st.expander("Деталі: що робить крок 3 (MathcURLs)", expanded=False):
        st.markdown(
            "Джерело — поточний CSV після **кроку 2** (`sheets_preparation_out_bytes`). Для рядків із валідним email "
            "порівнюються **Website** і **домен з адреси** (та сама логіка, що на вкладці MathcURLs: HTTP, за потреби Chromium). "
            "Колонка **Domain** — одразу після **Email**, **Results** — одразу після **Domain**. "
            "У **Results**: «Залишено (Matched / …)» або «Видалено (One of websites is dead / …)» — той самий текст статусу, що в output MathcURLs. "
            "Рядки **без** валідного домена в email: Domain і Results порожні."
        )

    gate_src = st.session_state.get("sheets_preparation_out_bytes")

    if not st.session_state.get("sheets_prep_gate_requested", False) and not gate_busy:
        if st.button(
            "Запустити крок 3 (Website ↔ домен email)",
            type="primary",
            key="sheets_prep_gate_btn",
            disabled=not gate_src,
            help=None
            if gate_src
            else "Спочатку виконайте **крок 1 і крок 2** (або **Підставити останній CSV знову** / файл нижче).",
        ):
            st.session_state["sheets_prep_gate_requested"] = True
            st.session_state.pop("sheets_prep_gate_out_bytes", None)
            st.session_state.pop("sheets_prep_gate_log", None)
            st.session_state.pop("sheets_prep_step4_out_bytes", None)
            st.session_state.pop("sheets_prep_step4_log", None)
            st.rerun()

    if not gate_src:
        if buf_for_gate is None:
            st.caption(
                "Спочатку виконайте **крок 1 і крок 2** — тоді CSV підставиться автоматично "
                "(або **Підставити останній CSV знову** / завантажте файл нижче)."
            )
        else:
            st.caption(
                "Немає CSV для кроку 3 у сесії. Натисніть **Підставити останній CSV знову** або завантажте файл нижче."
            )

    gate_upload = st.file_uploader(
        "Або завантажте CSV з диска (наприклад вихід після кроку 2)",
        type=["csv"],
        key="sheets_prep_gate_file",
        help="Якщо буфер порожній або потрібен інший файл.",
    )
    if gate_upload is not None:
        _sig = (gate_upload.name, getattr(gate_upload, "size", None), len(gate_upload.getvalue()))
        if st.session_state.get("_sheets_prep_gate_upload_sig") != _sig:
            st.session_state["_sheets_prep_gate_upload_sig"] = _sig
            st.session_state["sheets_preparation_out_bytes"] = gate_upload.getvalue()
            st.session_state["sheets_prep_gate_input_at"] = datetime.now().isoformat(
                timespec="seconds"
            )
            st.session_state["sheets_prep_preview_after_step"] = 2
            st.session_state.pop("sheets_prep_gate_out_bytes", None)
            st.session_state.pop("sheets_prep_gate_log", None)
            st.session_state.pop("sheets_prep_step4_out_bytes", None)
            st.session_state.pop("sheets_prep_step4_log", None)

    if st.session_state.get("sheets_prep_gate_requested"):
        st.session_state["sheets_prep_gate_requested"] = False
        raw_gate = st.session_state.get("sheets_preparation_out_bytes") or b""
        if not raw_gate:
            st.error("Немає CSV після кроку 2.")
        else:
            stop_ev = threading.Event()
            holder: dict = {
                "p": 0.0,
                "msg": "Підготовка…",
                "bytes": None,
                "log": "",
                "error": None,
                "stopped": False,
                "finished": False,
            }
            fb = bytes(raw_gate)

            def worker() -> None:
                try:

                    def on_prog(p: float, msg: str) -> None:
                        holder["p"] = float(p)
                        holder["msg"] = msg

                    b, lg = email_domain_gate_to_csv_bytes(
                        fb,
                        on_progress=on_prog,
                        should_stop=stop_ev.is_set,
                    )
                    holder["bytes"] = b
                    holder["log"] = lg
                except EmailDomainGateStopped:
                    holder["stopped"] = True
                    holder["log"] = (
                        "Зупинка до завершення перевірки — CSV не оновлено; запустіть крок знову."
                    )
                except Exception as exc:
                    holder["error"] = str(exc)
                finally:
                    holder["finished"] = True

            th = threading.Thread(target=worker, daemon=True, name="sheets-prep-gate")
            th.start()
            st.session_state["sheets_prep_gate_bg_running"] = True
            st.session_state["sheets_prep_gate_thread"] = th
            st.session_state["sheets_prep_gate_stop_ev"] = stop_ev
            st.session_state["sheets_prep_gate_holder"] = holder
            st.rerun()

    if st.session_state.get("sheets_prep_gate_bg_running"):
        th_gate: threading.Thread = st.session_state["sheets_prep_gate_thread"]
        holder_gate = st.session_state["sheets_prep_gate_holder"]
        stop_gate: threading.Event = st.session_state["sheets_prep_gate_stop_ev"]

        if th_gate.is_alive():
            pg = float(holder_gate.get("p") or 0.0)
            st.progress(min(max(pg, 0.0), 1.0))
            st.caption(str(holder_gate.get("msg") or ""))
            if st.button(
                "Зупинити (крок 3)",
                type="secondary",
                key="sheets_prep_gate_stop_btn",
                help="Сигнал між рядками перевірки; поточна пара може бути дорахована до кінця.",
            ):
                stop_gate.set()
                st.rerun()
            st.caption("Крок 4 нижче вже на сторінці — після завершення кроку 3 підставте буфер у крок 4.")
        else:
            st.session_state["sheets_prep_gate_bg_running"] = False
            err_g = holder_gate.get("error")
            if err_g:
                st.error(f"Крок 3: {err_g}")
            elif holder_gate.get("stopped"):
                st.warning(holder_gate.get("log") or "Зупинено.")
            else:
                st.session_state["sheets_prep_gate_out_bytes"] = holder_gate.get("bytes") or b""
                st.session_state["sheets_prep_gate_log"] = holder_gate.get("log") or ""
                _autosave_after_step3()
                st.session_state["sheets_prep_step4_input_at"] = st.session_state.get(
                    "sheets_prep_buffer_saved_at"
                )
                tg_g = notify_task_finished("Sheets Preparation — крок 3")
                st.session_state["sheets_prep_gate_tg_err"] = tg_g
            st.session_state.pop("sheets_prep_gate_thread", None)
            st.session_state.pop("sheets_prep_gate_stop_ev", None)
            st.session_state.pop("sheets_prep_gate_holder", None)
            st.rerun()

    if st.session_state.get("sheets_prep_gate_out_bytes") is not None:
        st.success("Крок 3 завершено — нижче завантаження CSV з колонками Domain та Results.")
        st.caption(
            "Результат збережено в **буфер сесії** — кнопка **«Підставити останній CSV знову»** у **кроку 3** або **кроку 4** підставить цей файл."
        )
        tg_ge = st.session_state.get("sheets_prep_gate_tg_err")
        if tg_ge:
            st.caption(f"Telegram: {tg_ge}")
        st.download_button(
            "Завантажити CSV після кроку 3 (Website ↔ домен)",
            data=st.session_state["sheets_prep_gate_out_bytes"],
            file_name="sheets_preparation_after_domain_gate.csv",
            mime="text/csv",
            key="sheets_prep_gate_dl",
        )
        with st.expander("Журнал (крок 3)", expanded=False):
            st.code(st.session_state.get("sheets_prep_gate_log") or "", language="text")

    st.divider()
    st.markdown("### Крок 4: Видалити погані емейли")
    st.markdown("**Крок 4 — підставити CSV з буфера:**")
    st.caption(
        "Потрібен файл **після кроку 3** (колонки Domain та Results). Натисніть **Підставити останній CSV знову** — у вхід піде "
        "останній збережений у сесії CSV (зазвичай це вихід кроку 3). Деталі логіки — у розгорнутому блоці."
    )

    buf_for_step4 = st.session_state.get("sheets_prep_buffer_bytes")
    if st.button(
        "Підставити останній CSV знову",
        key="sheets_prep_step4_reload",
        help="Підставити останній CSV з буфера у вхід кроку 4 (очікується вихід кроку 3 з Domain/Results).",
        use_container_width=True,
    ):
        if buf_for_step4 is None:
            st.warning("У буфері сесії ще немає CSV. Спочатку виконайте крок 3 або завантажте файл нижче.")
        else:
            st.session_state["sheets_prep_gate_out_bytes"] = bytes(buf_for_step4)
            st.session_state["sheets_prep_step4_input_at"] = datetime.now().isoformat(
                timespec="seconds"
            )
            st.session_state.pop("sheets_prep_step4_out_bytes", None)
            st.session_state.pop("sheets_prep_step4_log", None)
            st.success("CSV для кроку 4 оновлено з буфера.")

    _g4 = st.session_state.get("sheets_prep_gate_out_bytes")
    if _g4:
        _ts4 = st.session_state.get("sheets_prep_step4_input_at") or "—"
        st.caption(f"Для кроку 4: **{len(_g4):,}** байт · `{_ts4}`")

    with st.expander("Деталі: що робить крок 4", expanded=False):
        st.markdown(
            "Якщо в **Results** — «Видалено (One of websites is dead)» або «Видалено (No Redirect or not Match)», "
            "у цьому рядку очищаються лише **Email** і **Domain**. **Results не змінюємо**. "
            "Після виконання результат зберігається в **буфер сесії**."
        )

    step4_upload = st.file_uploader(
        "Або завантажте CSV після кроку 3 з диска",
        type=["csv"],
        key="sheets_prep_step4_file",
        help="Якщо потрібен конкретний файл з Domain та Results.",
    )
    if step4_upload is not None:
        _sig4 = (step4_upload.name, getattr(step4_upload, "size", None), len(step4_upload.getvalue()))
        if st.session_state.get("_sheets_prep_step4_upload_sig") != _sig4:
            st.session_state["_sheets_prep_step4_upload_sig"] = _sig4
            st.session_state["sheets_prep_gate_out_bytes"] = step4_upload.getvalue()
            st.session_state["sheets_prep_step4_input_at"] = datetime.now().isoformat(
                timespec="seconds"
            )
            st.session_state.pop("sheets_prep_step4_out_bytes", None)
            st.session_state.pop("sheets_prep_step4_log", None)

    gate_for_step4 = st.session_state.get("sheets_prep_gate_out_bytes")
    step4_ready = gate_for_step4 is not None and len(gate_for_step4) > 0

    if not step4_ready:
        st.caption(
            "Щоб увімкнути **Видалити погані емейли**: виконайте крок 3 або натисніть **Підставити останній CSV знову** / завантажте CSV вище."
        )

    if st.button(
        "Видалити погані емейли",
        type="primary",
        key="sheets_prep_step4_btn",
        disabled=not step4_ready,
        help=None
        if step4_ready
        else "Підставте CSV з буфера / з диска (потрібен вихід кроку 3: Domain та Results).",
    ):
        try:
            out_b, log_s4 = strip_bad_emails_from_csv_bytes(bytes(gate_for_step4))
            st.session_state["sheets_prep_step4_out_bytes"] = out_b
            st.session_state["sheets_prep_step4_log"] = log_s4
            _autosave_after_step4()
            tg_s4 = notify_task_finished("Sheets Preparation — крок 4")
            st.session_state["sheets_prep_step4_tg_err"] = tg_s4
        except Exception as exc:
            st.error(f"Крок 4: {exc}")
        else:
            st.rerun()

    if st.session_state.get("sheets_prep_step4_out_bytes") is not None:
        _render_current_output_preview_and_download(
            dl_key="sheets_prep_step4_dl",
            step_label="після кроку 4",
            data_bytes=st.session_state["sheets_prep_step4_out_bytes"],
            log_text=st.session_state.get("sheets_prep_step4_log") or "",
            expander_title="Журнал (крок 4)",
            download_label="Завантажити CSV після кроку 4",
            file_name="sheets_preparation_after_step4_strip_bad_emails.csv",
            show_tg_caption=True,
            tg_session_key="sheets_prep_step4_tg_err",
            caption_after_success=(
                "Файл збережено в **буфер сесії** — під кроком 2 кнопка **«Підставити останній CSV знову»** "
                "підставить цей CSV."
            ),
        )

    if st.session_state.get("sheets_prep_ai_bg_running"):
        _th_poll = st.session_state.get("sheets_prep_ai_thread")
        if _th_poll is not None and _th_poll.is_alive():
            time.sleep(1.0)
            st.rerun()
    if st.session_state.get("sheets_prep_gate_bg_running"):
        _tg_poll = st.session_state.get("sheets_prep_gate_thread")
        if _tg_poll is not None and _tg_poll.is_alive():
            time.sleep(1.0)
            st.rerun()
