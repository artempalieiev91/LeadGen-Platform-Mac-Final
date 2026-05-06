import streamlit as st

from auth.gate import require_login
from services.platform_openai import (
    SESSION_FLAG_CLEAR_OPENAI_FIELD,
    apply_pending_openai_field_clear,
    delete_stored_openai_key,
    hydrate_openai_key_widget_from_keyring,
    openai_api_key_effective,
    openai_api_key_looks_valid,
    save_openai_key_to_keyring,
)
from services.telegram_notify import send_connect_test_message
from tabs.name2emails import render_name2emails
from tabs.research_validation import render_research_validation
from tabs.sheets_preparation import render_sheets_preparation

st.set_page_config(
    page_title="LeadGen Platform",
    page_icon="🔍",
    layout="wide",
)

require_login()

TELEGRAM_BOT_LINK = "https://t.me/leadgenCodeIT_bot"

if "platform_openai_migrated" not in st.session_state:
    if not (st.session_state.get("platform_openai_api_key") or "").strip():
        for _legacy in ("rv_openai_api_key", "sheets_prep_openai_key"):
            _old = (st.session_state.get(_legacy) or "").strip()
            if _old:
                st.session_state["platform_openai_api_key"] = _old
                break
    st.session_state["platform_openai_migrated"] = True

hydrate_openai_key_widget_from_keyring()

with st.sidebar:
    st.markdown("**Telegram**")
    st.caption(
        f"Бот для сповіщень: [@leadgenCodeIT_bot]({TELEGRAM_BOT_LINK}) — спочатку **/start** у цьому чаті."
    )
    st.text_input(
        "Нікнейм / Chat ID або @username",
        key="telegram_chat_id",
        placeholder="123456789 або @nickname",
        help=(
            "У Secrets має бути токен саме цього бота. Після /start натисніть Connect. "
            "Якщо помилка — вставте числовий id з @userinfobot."
        ),
    )
    if st.button("Connect", key="telegram_connect_btn", use_container_width=True):
        with st.spinner("Запит до Telegram…"):
            ok, err = send_connect_test_message()
        if ok:
            st.success("Повідомлення надіслано — перевірте Telegram.")
        else:
            st.error(err or "Помилка відправки.")

st.title("LeadGen Platform 🔍")
st.caption("Інструменти на окремих вкладках.")

st.markdown("---")
st.subheader("OpenAI")
st.caption(
    "Один ключ для Research Validation, Sheets Preparation (крок 2 — AI) тощо. "
    "**Зберегти на пристрої** — Keychain / Windows Credential Manager; після F5 ключ підставиться знову. "
    "Або `openai_api_key` у Secrets."
)
apply_pending_openai_field_clear()
st.text_input(
    "API key OpenAI",
    type="password",
    key="platform_openai_api_key",
    placeholder="sk-...",
)
_k_eff = openai_api_key_effective()
if _k_eff and len(_k_eff) >= 12 and not openai_api_key_looks_valid(_k_eff):
    st.warning(
        "Ключ не схожий на **OpenAI API key** (очікується рядок, що починається з **sk-**). "
        "Скопіюйте секрет із [API keys](https://platform.openai.com/account/api-keys), без лапок і зайвих рядків. "
        "Якщо ключ уже правильний — перевірте, що він не відкликаний і що в Secrets немає іншого `openai_api_key`."
    )
oc1, oc2 = st.columns(2)
with oc1:
    if st.button("Зберегти на пристрої", key="platform_openai_save_keyring", type="primary"):
        _k = (st.session_state.get("platform_openai_api_key") or "").strip()
        if not _k:
            st.error("Введіть ключ перед збереженням.")
        elif save_openai_key_to_keyring(_k):
            st.success("Збережено в системному сховищі. Після перезавантаження сторінки ключ підставиться автоматично.")
        else:
            st.error("Не вдалося зберегти. Перевірте: `pip install keyring`.")
with oc2:
    if st.button("Видалити збережений ключ", key="platform_openai_forget_keyring"):
        delete_stored_openai_key()
        st.session_state[SESSION_FLAG_CLEAR_OPENAI_FIELD] = True
        st.success("Збережений ключ видалено з пристрою. Поле очищено.")
        st.rerun()
st.markdown("---")

tab_rv, tab_sheets, tab_n2e = st.tabs(
    ["Research Validation", "Sheets Preparation", "Name2Emails"]
)

with tab_rv:
    render_research_validation()

with tab_sheets:
    render_sheets_preparation()

with tab_n2e:
    render_name2emails()
