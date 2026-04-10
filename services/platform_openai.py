"""
Спільний OpenAI API key для платформи: поле на головній сторінці, Streamlit Secrets,
опційно — збереження в системному сховищі (macOS Keychain тощо), як для пароля входу.
"""

from __future__ import annotations

import streamlit as st

SESSION_KEY_OPENAI = "platform_openai_api_key"
# Прапор: на наступному rerun очистити поле ключа (після видалення з keyring; не можна змінювати
# SESSION_KEY_OPENAI після створення st.text_input з тим самим key у тому ж run).
SESSION_FLAG_CLEAR_OPENAI_FIELD = "_platform_openai_clear_field"

# Той самий сервіс, що й у auth.gate (keyring)
_KEYRING_SERVICE = "StreamlitPlatform"
_KEYRING_ACCOUNT = "openai_api_key"


def _keyring_get_openai() -> str | None:
    try:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
    except Exception:
        return None


def _keyring_set_openai(key: str) -> bool:
    try:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, key)
        return True
    except Exception:
        return False


def _keyring_delete_openai() -> bool:
    try:
        import keyring

        keyring.delete_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        return True
    except Exception:
        # Немає запису або помилка сховища — для «видалити» вважаємо завершеним
        return True


def normalize_openai_api_key(raw: str | None) -> str:
    """
    Прибирає типові помилки копіювання: BOM, зайві лапки, перенос рядка після sk-…
    Дійсні секретні ключі OpenAI починаються з sk- (див. platform.openai.com).
    """
    if raw is None:
        return ""
    s = str(raw).replace("\ufeff", "").strip()
    if not s:
        return ""
    if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
        s = s[1:-1].strip()
    if "\n" in s or "\r" in s:
        s = s.splitlines()[0].strip()
    s = s.strip()
    return s


def openai_api_key_looks_valid(key: str) -> bool:
    """Перевірка формату: очікується префікс sk- (user / project keys)."""
    k = normalize_openai_api_key(key)
    return bool(k) and k.startswith("sk-") and len(k) >= 20


def get_stored_openai_key() -> str | None:
    """Ключ із Keychain / Credential Manager, якщо раніше збережено."""
    raw = _keyring_get_openai()
    if raw is None:
        return None
    s = normalize_openai_api_key(str(raw))
    return s if s else None


def save_openai_key_to_keyring(key: str) -> bool:
    """Зберегти API key локально (до видалення або перезапису)."""
    k = normalize_openai_api_key(key)
    if not k:
        return False
    return _keyring_set_openai(k)


def delete_stored_openai_key() -> bool:
    """Прибрати збережений ключ із сховища пристрою."""
    return _keyring_delete_openai()


def hydrate_openai_key_widget_from_keyring() -> None:
    """Один раз за сесію підставити збережений ключ у поле, якщо поле ще не ініціалізоване."""
    if SESSION_KEY_OPENAI in st.session_state:
        return
    stored = get_stored_openai_key()
    st.session_state[SESSION_KEY_OPENAI] = stored if stored is not None else ""


def apply_pending_openai_field_clear() -> None:
    """Викликати перед st.text_input(..., key=SESSION_KEY_OPENAI), якщо користувач щойно видалив ключ."""
    if st.session_state.pop(SESSION_FLAG_CLEAR_OPENAI_FIELD, False):
        st.session_state[SESSION_KEY_OPENAI] = ""


def openai_api_key_from_secrets() -> str:
    try:
        return normalize_openai_api_key(str(st.secrets.get("openai_api_key", "") or ""))
    except Exception:
        return ""


def openai_api_key_effective() -> str:
    """Ключ з поля на головній сторінці або `openai_api_key` у `.streamlit/secrets.toml`."""
    field = normalize_openai_api_key(st.session_state.get(SESSION_KEY_OPENAI))
    if field:
        return field
    return openai_api_key_from_secrets()


def configure_openai_http_client(client: object) -> None:
    """
    httpx кодує значення заголовків як ASCII. OpenAI SDK підставляє в заголовки
    OpenAI-Organization / OpenAI-Project з OPENAI_ORG_ID / OPENAI_PROJECT_ID та Bearer-ключ;
    не-ASCII (наприклад випадкова кирилиця в env або в ключі) дає UnicodeEncodeError
    на кроці відправки запиту.
    """
    org = getattr(client, "organization", None)
    if org is not None and not str(org).isascii():
        client.organization = None
    proj = getattr(client, "project", None)
    if proj is not None and not str(proj).isascii():
        client.project = None
    key = getattr(client, "api_key", None)
    if isinstance(key, str) and key and not key.isascii():
        client.api_key = "".join(ch for ch in key if ch.isascii())
