"""Вкладка MathcURLs — обгортка над vendor/mathcurls (match_urls)."""

from __future__ import annotations

import threading
import time

import streamlit as st

from services.mathcurls_run import run_mathcurls_pipeline
from services.telegram_notify import notify_task_finished


def render_mathcurls() -> None:
    st.subheader("MathcURLs")
    st.markdown(
        """
**Як користуватися.** Завантажте CSV, у якому в кожному рядку два стовпці з URL. Сервіс перевіряє пари посилань 
(спочатку через HTTP-редиректи, за потреби — у браузері) і формує таблицю результатів.  
Після натискання «Запустити обробку» з’явиться файл **output.csv** для завантаження та журнал виконання.

**Зупинка:** під час роботи можна натиснути «Зупинити зі збереженням» — зберігається частковий **output.csv** (рядки, оброблені до зупинки; перевірка відбувається між рядками).

**Якщо вимкнути комп’ютер або засне ОС:** процес зупиняється — обробка **не** триває у фоні. Потрібен увімкнений ПК або сервер без сну на час задачі.
        """
    )

    uploaded = st.file_uploader(
        "Вхідний CSV",
        type=["csv"],
        key="mathcurls_file_uploader",
        help="Два стовпці з URL у кожному рядку.",
    )

    if uploaded is not None:
        st.session_state["mathcurls_data"] = uploaded.getvalue()
        st.session_state["mathcurls_name"] = uploaded.name

    has_input = bool(st.session_state.get("mathcurls_data"))
    has_output = st.session_state.get("mathcurls_output_bytes") is not None

    if not has_input and not has_output:
        st.info("Оберіть файл CSV.")
        return

    st.session_state.setdefault("mathcurls_run_requested", False)
    st.session_state.setdefault("mathcurls_bg_running", False)

    if uploaded is not None:
        st.caption(f"**{uploaded.name}** · {uploaded.size:,} байт")
    else:
        name = st.session_state.get("mathcurls_name") or "input.csv"
        st.caption(f"**{name}** · файл з попереднього завантаження в сесії")

    busy = st.session_state.get("mathcurls_bg_running", False)

    if not st.session_state["mathcurls_run_requested"] and not busy:
        if st.button(
            "Запустити обробку",
            type="primary",
            key="mathcurls_run_btn",
            disabled=not has_input,
            help=None if has_input else "Спочатку завантажте CSV — дані зберігаються в сесії після вибору файлу.",
        ):
            st.session_state["mathcurls_run_requested"] = True
            st.session_state.pop("mathcurls_output_bytes", None)
            st.session_state.pop("mathcurls_last_log", None)
            st.rerun()

    if st.session_state["mathcurls_run_requested"]:
        st.session_state["mathcurls_run_requested"] = False
        file_bytes = st.session_state.get("mathcurls_data") or b""
        if not file_bytes:
            st.error("Немає даних CSV.")
            return

        stop_ev = threading.Event()
        holder: dict = {"p": 0.0, "msg": "Підготовка…", "bytes": None, "log": "", "error": None}

        def worker() -> None:
            try:
                b, lg = run_mathcurls_pipeline(
                    file_bytes,
                    should_stop=stop_ev.is_set,
                    progress_holder=holder,
                )
                holder["bytes"] = b
                holder["log"] = lg
            except Exception as exc:
                holder["error"] = str(exc)
            finally:
                holder["finished"] = True

        th = threading.Thread(target=worker, daemon=True, name="mathcurls-pipeline")
        th.start()
        st.session_state["mathcurls_bg_running"] = True
        st.session_state["mathcurls_thread"] = th
        st.session_state["mathcurls_stop_ev"] = stop_ev
        st.session_state["mathcurls_holder"] = holder
        st.rerun()

    if st.session_state.get("mathcurls_bg_running"):
        th: threading.Thread = st.session_state["mathcurls_thread"]
        holder = st.session_state["mathcurls_holder"]
        stop_ev: threading.Event = st.session_state["mathcurls_stop_ev"]

        p = float(holder.get("p") or 0.0)
        prog = st.progress(min(max(p, 0.0), 1.0))
        lbl = st.empty()
        lbl.caption(str(holder.get("msg") or ""))

        if st.button(
            "Зупинити зі збереженням",
            type="secondary",
            key="mathcurls_stop_btn",
            help="Передає сигнал зупинки між рядками; поточний рядок може бути дорахований до кінця.",
        ):
            stop_ev.set()
            st.rerun()

        if not th.is_alive():
            st.session_state["mathcurls_bg_running"] = False
            err = holder.get("error")
            if err:
                prog.progress(0)
                lbl.caption("")
                st.error(f"Помилка: {err}")
            else:
                st.session_state["mathcurls_output_bytes"] = holder.get("bytes") or b""
                st.session_state["mathcurls_last_log"] = holder.get("log") or ""
                tg_err = notify_task_finished("MathcURLs")
                if tg_err:
                    st.session_state["mathcurls_tg_err"] = tg_err
                else:
                    st.session_state.pop("mathcurls_tg_err", None)
            st.session_state.pop("mathcurls_thread", None)
            st.session_state.pop("mathcurls_stop_ev", None)
            st.session_state.pop("mathcurls_holder", None)
            st.rerun()

        time.sleep(1.0)
        st.rerun()

    if st.session_state.get("mathcurls_output_bytes") is not None:
        log_text = st.session_state.get("mathcurls_last_log") or ""
        partial = "[Зупинка]" in log_text or "Зупинка" in log_text
        if partial:
            st.warning("Збережено частковий output.csv (див. журнал).")
        else:
            st.success("Обробку завершено.")
        tg_err = st.session_state.get("mathcurls_tg_err")
        if tg_err:
            st.caption(f"Не вдалося надіслати сповіщення в Telegram: {tg_err}")
        st.download_button(
            label="Завантажити output.csv",
            data=st.session_state["mathcurls_output_bytes"],
            file_name="output.csv",
            mime="text/csv",
            key="mathcurls_download",
        )
        with st.expander("Журнал виконання", expanded=False):
            st.code(log_text or "(порожній лог)", language="text")
