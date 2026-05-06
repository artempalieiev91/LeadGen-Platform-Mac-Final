"""Вкладка Research Validation — OpenAI, промпти, CSV (крок 1 + крок 2 за логікою analyze_sites)."""

from __future__ import annotations

import uuid

import streamlit as st

from services.keep_awake import prevent_idle_sleep
from services.research_validation import (
    ANALYSIS_ORDER_FIRST_DESCRIPTION_THEN_WEBSITE,
    ANALYSIS_ORDER_FIRST_WEBSITE_THEN_DESCRIPTION,
    MARKER_NEED_SITE,
    MARKER_NOT_RELEVANT,
    MARKER_RELEVANT,
    research_validation_state_finalize,
    research_validation_state_step,
    research_validation_validate_and_init_state,
)
from services.research_validation_prompts import load_prompts, save_prompts
from services.platform_openai import openai_api_key_effective
from services.telegram_notify import notify_task_finished


def _ensure_prompts_state() -> list[dict]:
    if "rv_prompts_list" not in st.session_state:
        st.session_state["rv_prompts_list"] = load_prompts()
    return st.session_state["rv_prompts_list"]


def _rv_prompt_selection_changed() -> None:
    st.session_state["rv_ui_panel"] = None


def render_research_validation() -> None:
    st.subheader("Research Validation")

    if "rv_openai_model" not in st.session_state:
        st.session_state["rv_openai_model"] = "gpt-4o-mini"

    st.markdown(
        f"""
**Як працює інструмент.** Ключ OpenAI вводиться **на головній сторінці** (блок OpenAI під «LeadGen Platform») або через Secrets. Далі оберіть **модель**, додайте або оберіть **промпт**.

Завантажте **CSV** з колонками **Website** та **Short Description**. **Company Linkedin Url** і **Apollo Account Id** можна не мати — тоді у виході колонки 1–2 порожні, інакше значення **копіюються з того ж рядка** за назвою заголовка.

**Порядок аналізу** обирається нижче в блоці «CSV і запуск» (дві англомовні опції). Маркери в відповіді: `{MARKER_RELEVANT}`, `{MARKER_NOT_RELEVANT}`, `{MARKER_NEED_SITE}`. Формат сайту в промпті як у [OpenAI4omini-Python-Mac](https://github.com/artempalieiev91/OpenAI4omini-Python-Mac): **`Contents:`** + текст сторінки.

**Вихідний CSV — завжди 6 колонок** (порядок фіксований): **1** Company Linkedin Url · **2** Apollo Account Id · **3** Вебсайт (копія Website) · **4** Релевантність (для чіткого рішення **Так** / **Ні**, інакше службові статуси) · **5** Джерело рішення (сайт, лише CSV, запасний варіант після помилки — **без крапки з комою** в тексті, щоб Excel з роздільником `;` не з’їхав) · **6** Опис (маркери з відповіді прибираються).

Зберігайте результат як **UTF-8**; у Excel при імпорті вкажіть правильний роздільник (зазвичай **кома** для звичайного CSV). **Не перейменовуйте** ці шість заголовків, якщо далі йдуть інші кроки чи скрипти.

Під час довгої обробки можна **зупинити** — зберігаються вже готові рядки; решта позначаються як «Перервано». Зупинка між **блоками** рядків (див. «рядків за крок»).

**Якщо вимкнути комп’ютер або засне ОС:** локальний Streamlit зупиняється — потрібен сервер або вимкнення сну на час задачі.
        """
    )

    st.divider()
    st.markdown("### Модель OpenAI")
    st.text_input(
        "Модель",
        key="rv_openai_model",
        help="Наприклад: gpt-4o-mini, gpt-4o, o4-mini. Ключ API — на головній сторінці (OpenAI) або Secrets.",
    )

    api_key_effective = openai_api_key_effective()

    st.divider()
    st.markdown("### Промпти")

    prompts = _ensure_prompts_state()

    with st.expander("Додати новий промпт", expanded=False):
        t_title = st.text_input("Коротка назва", key="rv_new_prompt_title", placeholder="Напр.: MediBeat / SaaS")
        t_body = st.text_area("Текст промпту", key="rv_new_prompt_body", height=200, placeholder="Критерії релевантності та формат опису…")
        if st.button("Зберегти промпт", key="rv_save_new_prompt"):
            title = (t_title or "").strip() or "Без назви"
            body = (t_body or "").strip()
            if not body:
                st.error("Введіть текст промпту.")
            else:
                prompts.append({"id": str(uuid.uuid4()), "title": title, "text": body})
                save_prompts(prompts)
                st.session_state["rv_prompts_list"] = prompts
                st.success("Промпт додано.")
                st.rerun()

    selected_prompt_text = ""

    if prompts:
        titles = [p.get("title", "Без назви") for p in prompts]
        ix = st.selectbox(
            "Оберіть промпт (для перегляду, редагування, видалення та для запуску валідації)",
            range(len(prompts)),
            format_func=lambda i: titles[i],
            key="rv_prompt_pick",
            on_change=_rv_prompt_selection_changed,
        )
        selected_prompt_text = prompts[ix]["text"]

        b1, b2, b3 = st.columns(3)
        with b1:
            if st.button("Переглянути текст", key="rv_btn_view", use_container_width=True):
                st.session_state["rv_ui_panel"] = "view"
        with b2:
            if st.button("Редагувати", key="rv_btn_edit", use_container_width=True):
                pid = prompts[ix]["id"]
                st.session_state["rv_ui_panel"] = "edit"
                st.session_state["rv_editing_pid"] = pid
                st.session_state[f"rv_edit_title_{pid}"] = prompts[ix].get("title") or ""
                st.session_state[f"rv_edit_text_{pid}"] = prompts[ix].get("text") or ""
                st.rerun()
        with b3:
            if st.button("Видалити обраний промпт", key="rv_delete_btn", use_container_width=True):
                prompts.pop(ix)
                save_prompts(prompts)
                st.session_state["rv_prompts_list"] = prompts
                st.session_state["rv_ui_panel"] = None
                st.rerun()

        panel = st.session_state.get("rv_ui_panel")
        if panel == "view":
            st.text_area(
                "Повний текст промпту (лише перегляд)",
                value=prompts[ix]["text"],
                height=280,
                disabled=True,
                key=f"rv_readonly_prompt_{ix}",
            )
        elif panel == "edit":
            edit_pid = st.session_state.get("rv_editing_pid") or prompts[ix]["id"]
            if edit_pid != prompts[ix]["id"]:
                edit_pid = prompts[ix]["id"]
                st.session_state["rv_editing_pid"] = edit_pid
                st.session_state[f"rv_edit_title_{edit_pid}"] = prompts[ix].get("title") or ""
                st.session_state[f"rv_edit_text_{edit_pid}"] = prompts[ix].get("text") or ""

            st.text_input("Назва промпту", key=f"rv_edit_title_{edit_pid}")
            st.text_area("Текст промпту", key=f"rv_edit_text_{edit_pid}", height=260)
            s1, s2 = st.columns(2)
            with s1:
                if st.button("Зберегти зміни", key="rv_edit_save", type="primary"):
                    new_title = (st.session_state.get(f"rv_edit_title_{edit_pid}") or "").strip() or "Без назви"
                    new_text = (st.session_state.get(f"rv_edit_text_{edit_pid}") or "").strip()
                    if not new_text:
                        st.error("Текст промпту не може бути порожнім.")
                    else:
                        for p in prompts:
                            if p.get("id") == edit_pid:
                                p["title"] = new_title
                                p["text"] = new_text
                                break
                        save_prompts(prompts)
                        st.session_state["rv_prompts_list"] = prompts
                        st.session_state["rv_ui_panel"] = None
                        st.success("Промпт оновлено.")
                        st.rerun()
            with s2:
                if st.button("Скасувати редагування", key="rv_edit_cancel"):
                    st.session_state["rv_ui_panel"] = None
                    st.rerun()
    else:
        st.warning("Ще немає збережених промптів — додайте хоча б один у блоці вище.")
        st.text_area(
            "Тимчасовий промпт (лише для цього запуску, без збереження)",
            key="rv_ephemeral_prompt",
            height=180,
        )

    st.divider()
    st.markdown("### CSV і запуск")

    st.radio(
        "Порядок аналізу",
        options=[
            ANALYSIS_ORDER_FIRST_DESCRIPTION_THEN_WEBSITE,
            ANALYSIS_ORDER_FIRST_WEBSITE_THEN_DESCRIPTION,
        ],
        format_func=lambda v: (
            "First Description, then Website"
            if v == ANALYSIS_ORDER_FIRST_DESCRIPTION_THEN_WEBSITE
            else "First Website, then Description"
        ),
        key="rv_analysis_order",
        disabled=st.session_state.get("rv_active", False),
        help="First Description: спочатку лише Short Description і URL з рядка, за потреби — сайт і Contents. "
        "First Website: спочатку завантаження сторінки та AI по тексту, при невдачі — запасний аналіз лише по CSV.",
    )

    uploaded = st.file_uploader("Вхідний CSV", type=["csv"], key="rv_csv_uploader")

    if uploaded is not None:
        st.session_state["rv_csv_bytes"] = uploaded.getvalue()

    has_csv = uploaded is not None or bool(st.session_state.get("rv_csv_bytes"))

    if prompts:
        run_disabled = (
            not api_key_effective
            or not (selected_prompt_text or "").strip()
            or not has_csv
        )
    else:
        run_disabled = (
            not api_key_effective
            or not (st.session_state.get("rv_ephemeral_prompt") or "").strip()
            or not has_csv
        )

    st.session_state.setdefault("rv_run_requested", False)
    st.session_state.setdefault("rv_active", False)

    rows_per_chunk = st.slider(
        "Рядків за крок (між перезапусками інтерфейсу; менше — швидше реагує зупинка)",
        min_value=1,
        max_value=50,
        value=5,
        key="rv_chunk_slider_value",
        disabled=st.session_state.get("rv_active", False),
        help="Під час обробки змінити не можна. Зупинка спрацьовує після завершення поточного блоку.",
    )

    if not st.session_state["rv_run_requested"] and not st.session_state.get("rv_active"):
        if st.button("Запустити валідацію", type="primary", key="rv_run_btn", disabled=run_disabled):
            st.session_state["rv_run_requested"] = True
            st.rerun()

    if st.session_state["rv_run_requested"]:
        st.session_state["rv_run_requested"] = False
        prompt_body = selected_prompt_text if prompts else (st.session_state.get("rv_ephemeral_prompt") or "")
        model = (st.session_state.get("rv_openai_model") or "gpt-4o-mini").strip()
        csv_bytes = (
            uploaded.getvalue()
            if uploaded is not None
            else st.session_state.get("rv_csv_bytes") or b""
        )
        if not csv_bytes:
            st.error("Немає даних CSV. Завантажте файл знову.")
            return
        try:
            st.session_state["rv_state"] = research_validation_validate_and_init_state(
                csv_bytes,
                analysis_order=st.session_state.get(
                    "rv_analysis_order",
                    ANALYSIS_ORDER_FIRST_DESCRIPTION_THEN_WEBSITE,
                ),
            )
        except Exception as exc:
            st.error(f"Помилка CSV: {exc}")
            return
        st.session_state["rv_active"] = True
        st.session_state["rv_stop_requested"] = False
        st.session_state["rv_rows_per_chunk"] = int(rows_per_chunk)
        st.session_state["rv_partial"] = False
        st.session_state.pop("rv_out_bytes", None)
        st.session_state.pop("rv_last_log", None)
        st.session_state.pop("rv_tg_err", None)
        st.rerun()

    if st.session_state.get("rv_active"):
        state = st.session_state.get("rv_state")
        if not isinstance(state, dict) or "data_rows" not in state:
            st.session_state["rv_active"] = False
            st.error("Внутрішній стан втрачено. Запустіть валідацію знову.")
            st.rerun()

        n_rows = len(state["data_rows"])
        done = state["next_idx"]
        prompt_body = selected_prompt_text if prompts else (st.session_state.get("rv_ephemeral_prompt") or "")
        model = (st.session_state.get("rv_openai_model") or "gpt-4o-mini").strip()
        chunk = max(1, int(st.session_state.get("rv_rows_per_chunk") or 5))

        prog = st.progress(min(done / max(n_rows, 1), 1.0))
        st.caption(f"Оброблено **{done}** / **{n_rows}** рядків · блок по **{chunk}** рядків")

        if st.button("Зупинити зі збереженням", type="secondary", key="rv_stop_save"):
            st.session_state["rv_stop_requested"] = True
            st.rerun()

        if st.session_state.get("rv_stop_requested"):
            try:
                with prevent_idle_sleep():
                    out_bytes, log = research_validation_state_finalize(state, user_stopped=True)
            except Exception as exc:
                st.error(f"Помилка: {exc}")
                st.session_state["rv_active"] = False
                st.session_state.pop("rv_state", None)
                return
            st.session_state["rv_out_bytes"] = out_bytes
            st.session_state["rv_last_log"] = log
            st.session_state["rv_partial"] = True
            st.session_state["rv_active"] = False
            st.session_state["rv_stop_requested"] = False
            st.session_state.pop("rv_state", None)
            tg_err = notify_task_finished("Research Validation (зупинено)")
            st.session_state["rv_tg_err"] = tg_err
            st.rerun()

        if done >= n_rows:
            try:
                with prevent_idle_sleep():
                    out_bytes, log = research_validation_state_finalize(state, user_stopped=False)
            except Exception as exc:
                st.error(f"Помилка: {exc}")
                st.session_state["rv_active"] = False
                st.session_state.pop("rv_state", None)
                return
            st.session_state["rv_out_bytes"] = out_bytes
            st.session_state["rv_last_log"] = log
            st.session_state["rv_partial"] = False
            st.session_state["rv_active"] = False
            st.session_state.pop("rv_state", None)
            tg_err = notify_task_finished("Research Validation")
            st.session_state["rv_tg_err"] = tg_err
            st.rerun()

        lbl = st.empty()

        def report(p: float, msg: str) -> None:
            prog.progress(min(max(p, 0.0), 1.0))
            lbl.caption(msg)

        try:
            with prevent_idle_sleep():
                research_validation_state_step(
                    state,
                    user_prompt=prompt_body.strip(),
                    model=model,
                    api_key=api_key_effective,
                    max_rows=chunk,
                    on_progress=report,
                )
        except Exception as exc:
            prog.progress(0)
            lbl.caption("")
            st.error(f"Помилка: {exc}")
            st.session_state["rv_active"] = False
            st.session_state.pop("rv_state", None)
            return

        st.session_state["rv_state"] = state
        st.rerun()

    if st.session_state.get("rv_out_bytes"):
        if st.session_state.get("rv_partial"):
            st.warning("Обробку зупинено — у файлі є вже готові рядки; решта позначені як «Перервано».")
        else:
            st.success("Валідацію завершено.")
        te = st.session_state.get("rv_tg_err")
        if te:
            st.caption(f"Telegram: {te}")
        st.download_button(
            "Завантажити результат (CSV)",
            data=st.session_state["rv_out_bytes"],
            file_name="research_validation_output.csv",
            mime="text/csv",
            key="rv_dl",
        )
        with st.expander("Журнал", expanded=False):
            st.code(st.session_state.get("rv_last_log") or "", language="text")
