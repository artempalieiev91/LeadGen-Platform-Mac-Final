"""Вкладка Name2Emails — локальний клієнт Chrome + Gmail + Name2Email."""

from __future__ import annotations

import streamlit as st

from services.name2emails_table_prepare import prepare_emails_research_column
from services.name2emails_run import name2emails_supported_platform, run_name2email_client
from services.sheets_preparation_pipeline import parse_csv_bytes
from services.telegram_notify import notify_task_finished
from tabs.sheets_preparation import _write_session_csv_copies

# Як у vendor/name2emails/gmail_name2email_client.py → CHROME_DEBUG_PROFILE
_DEFAULT_CHROME_PROFILE = "~/chrome-debug-name2email"
_SESSION_CHROME_PROFILE = "name2emails_chrome_profile"
_SESSION_DATA = "name2emails_data"
_SESSION_NAME = "name2emails_name"
_PREPARED_SNAPSHOT = "name2emails_prepared_out_bytes"
# Після «Підготовка таблиці» буфер оновлено — «Старт» бере CSV лише з буфера
_SESSION_RUN_READY = "name2emails_run_ready"
# Останні байти з file_uploader — щоб після rerun не затирати підготовлений CSV і не скидати прев’ю
_SESSION_UPLOADER_BYTES = "name2emails_last_uploader_bytes"

# Той самий буфер, що оновлюється в Sheets Preparation (_write_session_csv_copies)
_SHEETS_BUFFER = "sheets_prep_buffer_bytes"
_SHEETS_BUFFER_AT = "sheets_prep_buffer_saved_at"
_SHEETS_FOR_AI = "sheets_prep_for_ai_bytes"
_SHEETS_FOR_AI_AT = "sheets_prep_for_ai_at"
# Одноразове повідомлення після «Завантажити з буфера»
_SESSION_BUFFER_LOADED_FLASH = "name2emails_buffer_loaded_flash"

_PREVIEW_MAX_ROWS = 10


def _unique_preview_headers(header: list[str]) -> list[str]:
    """Унікальні ключі для прев’ю (як у Sheets Preparation)."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for h in header:
        base = str(h).strip() or "(без назви)"
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append(base if n == 0 else f"{base} ({n + 1})")
    return out


def _render_prepared_table_preview(data_bytes: bytes) -> None:
    """Прев’ю та завантаження після «Підготовка таблиці» — логіка як після кроку 1 у Sheets Preparation."""
    if not data_bytes:
        return

    try:
        rows = parse_csv_bytes(data_bytes)
    except Exception:
        rows = []

    n_data = max(0, len(rows) - 1)
    st.divider()
    st.markdown("#### Поточний результат · після підготовки таблиці")
    st.success(
        f"Таблиця готова: **{n_data}** рядків даних · **{len(data_bytes):,}** байт CSV."
    )

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

    st.download_button(
        "Завантажити CSV після підготовки таблиці",
        data=data_bytes,
        file_name="name2emails_after_table_prepare.csv",
        mime="text/csv",
        key="name2emails_dl_prepared",
    )
    with st.expander("Примітки (підготовка таблиці)", expanded=False):
        st.caption(
            "Додано стовпчик **Emails Research** одразу після **Email**. Результат збережено в **буфері сесії**; "
            "для пошуку в Gmail натисніть **Старт** (він читає саме цей буфер)."
        )


def _name2e_status_ua(status: str) -> str:
    return {
        "init": "Підключення до Chrome і Gmail…",
        "found": "знайдено email",
        "not_found": "немає email",
        "skipped": "пропущено (поле «Кому»)",
    }.get(status, status)


def _csv_bytes_from_upload(uploaded) -> tuple[bytes | None, str | None]:
    """Файл з uploader або збережений у сесії після попереднього вибору."""
    if uploaded is not None:
        return uploaded.getvalue(), "завантажений файл"
    b = st.session_state.get(_SESSION_DATA)
    if b:
        return b, "збережений у сесії"
    return None, None


def render_name2emails() -> None:
    st.subheader("Name2Emails")

    if not name2emails_supported_platform():
        st.warning(
            "Цей блок працює лише коли Streamlit запущено **на вашому Mac** (`streamlit run` локально): "
            "потрібні Chrome, Gmail і Name2Email. На типовому хмарному хостингу (Linux, зокрема Streamlit Cloud) "
            "Python виконується на сервері, а не на вашому ПК — тому тут немає сенсу в завантаженні файлу й "
            "кнопці «Старт» для цього сценарію."
        )
        return

    st.markdown(
        """
**Як користуватися.** У другій колонці кожного рядка CSV — пошуковий запит 
(ім’я, компанія тощо у форматі, який підтримує Name2Email).  
**Завантажте Input.csv** нижче (UTF-8) або натисніть **«Завантажити з буфера»** — підставиться CSV з **буфера сесії** після кроків у **Sheets Preparation**.  
**Підготовка таблиці** додає праворуч від **Email** стовпчик **Emails Research** з рядком на кшталт `Ім'я Прізвище@Website` (еквівалент формули в Excel: First Name, пробіл, Last Name, символ @, Website). Після підготовки з’являється блок **«Поточний результат»** з прев’ю та завантаженням — як після **кроку 1** у Sheets Preparation; підготовлений CSV **зберігається в буфер сесії** (той самий, що й у Sheets Preparation).  
**Старт** запускає Name2Email **з буфера** (після підготовки), лише для рядків, де в **Email** ще порожньо; якщо емейл уже є — рядок не чіпається. Знайдену адресу записуємо в **Email**; якщо нічого не знайдено — у **Email** підставляється текст `email not found`. Результат — **Output_With_Emails.csv** (той самий набір колонок, що й у вхідному файлі).

**Зупинка / сон ПК:** сценарій керує локальним Chrome — зупинити можна закривши браузер або перервавши процес. Якщо **вимкнути ПК або засне ОС**, обробка не продовжиться у фоні.
        """
    )

    uploaded = st.file_uploader(
        "Завантажте Input.csv",
        type=["csv"],
        key="name2emails_file_local",
        help="Після вибору файл зберігається в сесії.",
    )
    if uploaded is not None:
        raw = uploaded.getvalue()
        prev_uploader = st.session_state.get(_SESSION_UPLOADER_BYTES)
        if prev_uploader != raw:
            st.session_state[_SESSION_DATA] = raw
            st.session_state[_SESSION_NAME] = uploaded.name
            st.session_state[_SESSION_UPLOADER_BYTES] = raw
            st.session_state.pop(_PREPARED_SNAPSHOT, None)
            st.session_state.pop(_SESSION_RUN_READY, None)

    has_sheets_buffer = bool(
        st.session_state.get(_SHEETS_BUFFER) or st.session_state.get(_SHEETS_FOR_AI)
    )
    if st.button(
        "Завантажити з буфера",
        key="name2emails_load_from_buffer",
        disabled=not has_sheets_buffer,
        help="Підставити CSV з буфера сесії (після кроків 1–4 у Sheets Preparation).",
    ):
        buf = st.session_state.get(_SHEETS_BUFFER) or st.session_state.get(_SHEETS_FOR_AI)
        if buf:
            b = bytes(buf)
            st.session_state[_SESSION_DATA] = b
            st.session_state[_SESSION_NAME] = "Input_from_sheets_buffer.csv"
            st.session_state[_SESSION_UPLOADER_BYTES] = b
            st.session_state.pop(_PREPARED_SNAPSHOT, None)
            st.session_state.pop(_SESSION_RUN_READY, None)
            ts = st.session_state.get(_SHEETS_BUFFER_AT) or st.session_state.get(_SHEETS_FOR_AI_AT)
            st.session_state[_SESSION_BUFFER_LOADED_FLASH] = (
                f"CSV з **буфера сесії** підставлено в Name2Emails: **{len(b):,}** байт"
                + (f" (мітка буфера: `{ts}`)" if ts else "")
                + ". Далі натисніть **Підготовка таблиці**, якщо ще не робили."
            )
            st.rerun()

    flash_buf = st.session_state.pop(_SESSION_BUFFER_LOADED_FLASH, None)
    if flash_buf:
        st.success(flash_buf)

    csv_for_prepare, _ = _csv_bytes_from_upload(uploaded)
    can_prepare = csv_for_prepare is not None
    if st.button(
        "Підготовка таблиці",
        type="secondary",
        key="name2emails_table_prepare_btn",
        disabled=not can_prepare,
        help="Додає стовпчик Emails Research одразу після Email (First Name + пробіл + Last Name + @ + Website).",
    ):
        raw, _ = _csv_bytes_from_upload(uploaded)
        if not raw:
            st.error("Немає CSV — завантажте файл або підставте з буфера Sheets Preparation.")
        else:
            new_b, err = prepare_emails_research_column(raw)
            if err:
                st.error(err)
            else:
                nb = bytes(new_b)
                st.session_state[_SESSION_DATA] = nb
                st.session_state[_SESSION_NAME] = "Input_prepared.csv"
                st.session_state[_PREPARED_SNAPSHOT] = nb
                now_s = _write_session_csv_copies(nb)
                st.session_state[_SHEETS_FOR_AI] = nb
                st.session_state[_SHEETS_FOR_AI_AT] = now_s
                st.session_state[_SESSION_RUN_READY] = True
                st.rerun()

    if st.session_state.get(_PREPARED_SNAPSHOT):
        _render_prepared_table_preview(st.session_state[_PREPARED_SNAPSHOT])

    if _SESSION_CHROME_PROFILE not in st.session_state:
        st.session_state[_SESSION_CHROME_PROFILE] = _DEFAULT_CHROME_PROFILE

    st.text_input(
        "Каталог профілю Chrome",
        key=_SESSION_CHROME_PROFILE,
        help=(
            "За замовчуванням — `~/chrome-debug-name2email` (те саме, що `CHROME_DEBUG_PROFILE` у "
            "`gmail_name2email_client.py`). Можна вказати інший шлях. Якщо поле повністю очистити й "
            "запустити — знову використається дефолт скрипта."
        ),
    )
    raw_profile = (st.session_state.get(_SESSION_CHROME_PROFILE) or "").strip()
    profile = raw_profile if raw_profile else None

    csv_bytes, source_label = _csv_bytes_from_upload(uploaded)
    has_input = csv_bytes is not None
    if has_input and source_label:
        nm = st.session_state.get(_SESSION_NAME) or "Input.csv"
        cap = f"Джерело: **{source_label}** · **{nm}** · {len(csv_bytes):,} байт"
        if source_label == "збережений у сесії" and nm == "Input_from_sheets_buffer.csv":
            ts = st.session_state.get(_SHEETS_BUFFER_AT)
            if ts:
                cap += f" · буфер: `{ts}`"
        st.caption(cap)

    buf_for_run = st.session_state.get(_SHEETS_BUFFER) or st.session_state.get(_SHEETS_FOR_AI)
    can_start = bool(st.session_state.get(_SESSION_RUN_READY)) and bool(buf_for_run)
    if st.session_state.get(_SESSION_RUN_READY) and buf_for_run:
        ts_buf = st.session_state.get(_SHEETS_BUFFER_AT) or st.session_state.get(_SHEETS_FOR_AI_AT)
        st.caption(
            f"**Старт** використовує **буфер сесії** після підготовки · {len(buf_for_run):,} байт"
            + (f" · `{ts_buf}`" if ts_buf else "")
        )

    start = st.button(
        "Старт",
        type="primary",
        key="name2emails_local_start",
        disabled=not can_start,
        help="Спочатку «Підготовка таблиці» (результат потрапляє в буфер), потім запуск з буфера.",
    )

    if not start:
        return

    st.session_state.pop(_PREPARED_SNAPSHOT, None)

    csv_bytes = st.session_state.get(_SHEETS_BUFFER) or st.session_state.get(_SHEETS_FOR_AI)
    if not csv_bytes:
        st.error("У буфері сесії немає CSV — натисніть «Підготовка таблиці».")
        return
    if not st.session_state.get(_SESSION_RUN_READY):
        st.error("Спочатку натисніть «Підготовка таблиці», щоб зберегти результат у буфер.")
        return

    prog = st.progress(0)
    lbl = st.empty()

    def _on_progress(current: int, total: int, query: str, email: str, status: str) -> None:
        if total <= 0:
            return
        if current <= 0:
            prog.progress(0.0)
            lbl.caption(f"**0 / {total}** — {_name2e_status_ua(status)}")
            return
        prog.progress(min(current / total, 1.0))
        q = (query or "").replace("\n", " ").strip()
        if len(q) > 72:
            q = q[:69] + "…"
        line = _name2e_status_ua(status)
        if email.strip():
            line += f" — {email.strip()}"
        lbl.caption(f"**{current} / {total}** — {line} — {q}")

    try:
        out_bytes, log = run_name2email_client(
            csv_bytes,
            chrome_profile_dir=profile,
            wait_for_login=False,
            on_progress=_on_progress,
        )
    except Exception as exc:
        prog.progress(0)
        lbl.caption("")
        st.error(str(exc))
        return

    prog.progress(1.0)
    lbl.caption("Готово.")

    tg_err = notify_task_finished("Name2Emails")
    if tg_err:
        st.caption(f"Не вдалося надіслати сповіщення в Telegram: {tg_err}")

    if out_bytes:
        st.success("Обробку завершено.")
        st.download_button(
            "Завантажити Output_With_Emails.csv",
            data=out_bytes,
            file_name="Output_With_Emails.csv",
            mime="text/csv",
            key="name2emails_local_dl",
        )
    else:
        st.warning("Файл результату відсутній або порожній — перегляньте журнал нижче.")

    with st.expander("Журнал виконання"):
        st.code(log or "—", language="text")
