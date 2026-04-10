"""Простий вхід за логіном/паролем із st.secrets ([auth] username / password)."""

from __future__ import annotations

import hmac
import streamlit as st

# Сервісне ім’я для macOS Keychain / Windows Credential Manager / Secret Service
_KEYRING_SERVICE = "StreamlitPlatform"


def _keyring_get(username: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, username)
    except Exception:
        return None


def _keyring_set(username: str, password: str) -> bool:
    try:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, username, password)
        return True
    except Exception:
        return False


def _keyring_delete(username: str) -> bool:
    try:
        import keyring

        keyring.delete_password(_KEYRING_SERVICE, username)
        return True
    except Exception:
        return False


def _credentials() -> tuple[str, str] | None:
    try:
        auth = st.secrets.get("auth", {})
        user = auth.get("username")
        pwd = auth.get("password")
        if user is None or pwd is None:
            return None
        return str(user).strip(), str(pwd).strip()
    except Exception:
        return None


def _passwords_match(entered: str, expected: str) -> bool:
    try:
        return hmac.compare_digest(
            entered.encode("utf-8"),
            expected.encode("utf-8"),
        )
    except Exception:
        return False


def require_login() -> None:
    """Викликати одразу після set_page_config. Без валідного входу зупиняє виконання (st.stop)."""
    creds = _credentials()
    if creds is None:
        st.error(
            "Автентифікація не налаштована. Додайте у **Secrets** (Streamlit Cloud) або у файл "
            "`.streamlit/secrets.toml` секцію `[auth]` з полями `username` та `password`."
        )
        st.code(
            "[auth]\nusername = \"...\"\npassword = \"...\"",
            language="toml",
        )
        st.stop()

    expected_user, expected_pwd = creds

    if st.session_state.get("auth_ok") is True:
        with st.sidebar:
            saved = _keyring_get(expected_user)
            has_saved = saved is not None and _passwords_match(saved, expected_pwd)
            if has_saved:
                st.caption("Автовхід увімкнено: пароль збережено на цьому пристрої (Keychain).")
            else:
                st.caption("Автовхід вимкнено — пароль не збережено локально.")

            with st.expander("Зберегти логін і пароль для наступних відвідувань", expanded=False):
                st.markdown(
                    "Введіть пароль ще раз — він буде збережений у **сховищі паролів** "
                    "(macOS Keychain / Windows). Наступного разу вхід відбудеться автоматично."
                )
                save_pwd = st.text_input(
                    "Пароль для збереження",
                    type="password",
                    key="gate_sidebar_save_password",
                )
                if st.button("Зберегти", key="gate_sidebar_save_btn", type="primary"):
                    if _passwords_match(save_pwd.strip(), expected_pwd):
                        if _keyring_set(expected_user, save_pwd.strip()):
                            st.success("Збережено. Оновіть сторінку або залишайтесь — автовхід вже активний.")
                            st.rerun()
                        else:
                            st.error("Не вдалося записати (встановіть: pip install keyring).")
                    else:
                        st.error("Пароль не збігається з паролем у Secrets.")

            c1, c2 = st.columns(2)
            with c1:
                if st.button("Вийти", key="gate_logout"):
                    st.session_state.auth_ok = False
                    # Не підставляти пароль з Keychain одразу після явного виходу
                    st.session_state["_auth_keyring_tried"] = True
                    st.rerun()
            with c2:
                if st.button("Забути збережений пароль", key="gate_forget", disabled=not has_saved):
                    _keyring_delete(expected_user)
                    st.success("Збережені дані видалено.")
                    st.rerun()
        return

    # Спроба тихого входу зі сховища паролів (один раз за сесію)
    if st.session_state.get("_auth_keyring_tried") is not True:
        st.session_state["_auth_keyring_tried"] = True
        saved = _keyring_get(expected_user)
        if saved is not None and _passwords_match(saved, expected_pwd):
            st.session_state.auth_ok = True
            st.rerun()

    st.title("Вхід")
    st.caption("Уведіть облікові дані, щоб відкрити платформу.")

    login = st.text_input("Логін", key="gate_login")
    password = st.text_input("Пароль", type="password", key="gate_password")
    remember = st.checkbox(
        "Запам’ятати логін і пароль на цьому пристрої",
        value=True,
        key="gate_remember",
        help="Зберігається у системному сховищі (Keychain). Підходить лише для вашого особистого Mac/ПК.",
    )

    if st.button("Увійти", type="primary", key="gate_submit"):
        entered_login = login.strip()
        entered_pwd = password.strip()
        if entered_login == expected_user and _passwords_match(entered_pwd, expected_pwd):
            st.session_state.auth_ok = True
            if remember:
                if not _keyring_set(expected_user, entered_pwd):
                    st.warning("Не вдалося зберегти пароль локально (потрібен пакет keyring).")
            else:
                _keyring_delete(expected_user)
            st.rerun()
        else:
            st.error("Невірний логін або пароль.")

    st.stop()
