"""Сповіщення в Telegram після завершення задачі (Bot API)."""

from __future__ import annotations

import os
import re

import requests

CONNECT_TEST_MESSAGE = "LeadGen Platform Connected"


def _sanitize_token(raw: str) -> str:
    s = (raw or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    return s


def load_bot_token_from_secrets() -> str:
    """Токен з [telegram] bot_token, або TELEGRAM_BOT_TOKEN, або змінна середовища."""
    import streamlit as st

    candidates: list[str] = []

    try:
        if "telegram" in st.secrets:
            tg = st.secrets["telegram"]
            if hasattr(tg, "get"):
                v = tg.get("bot_token")
            else:
                v = tg["bot_token"] if "bot_token" in tg else None
            if v:
                candidates.append(str(v))
    except Exception:
        pass

    try:
        v = st.secrets.get("TELEGRAM_BOT_TOKEN")
        if v:
            candidates.append(str(v))
    except Exception:
        pass

    env = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if env:
        candidates.append(env)

    for c in candidates:
        t = _sanitize_token(c)
        if t:
            return t
    return ""


def verify_bot_token(bot_token: str) -> tuple[bool, str]:
    """Перевірка токена через getMe."""
    token = _sanitize_token(bot_token)
    if not token:
        return False, "Токен порожній."
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        r = requests.get(url, timeout=15)
        data = r.json() if r.content else {}
        if data.get("ok"):
            return True, ""
        return False, str(data.get("description") or r.text or "getMe failed")[:400]
    except requests.RequestException as exc:
        return False, str(exc)


def _resolve_recipient_chat_id(bot_token: str, raw: str) -> tuple[str | None, str | None]:
    s = raw.strip()
    if not s:
        return None, "Порожній ідентифікатор."

    if re.fullmatch(r"-?\d+", s):
        return s, None

    chat_param = s if s.startswith("@") else "@" + s.lstrip("@")
    url = f"https://api.telegram.org/bot{_sanitize_token(bot_token)}/getChat"
    try:
        r = requests.get(url, params={"chat_id": chat_param}, timeout=15)
        data = r.json() if r.content else {}
        if r.status_code == 200 and data.get("ok"):
            rid = data.get("result", {}).get("id")
            if rid is not None:
                return str(rid), None
        desc = str(data.get("description") or data.get("reason") or "Not Found")
        hint = (
            " Спочатку відкрийте бота leadgenCodeIT_bot у Telegram, надішліть /start, потім Connect знову "
            "або вставте числовий chat id (наприклад з @userinfobot)."
        )
        return None, f"{desc}.{hint}"
    except requests.RequestException as exc:
        return None, str(exc)


def send_telegram_message(bot_token: str, chat_id: str, text: str, timeout: float = 20.0) -> tuple[bool, str | None]:
    token = _sanitize_token(bot_token)
    if not token or not chat_id.strip():
        return False, "Немає токена або отримувача."

    ok_me, me_err = verify_bot_token(token)
    if not ok_me:
        return False, f"Токен бота недійсний або прострочений: {me_err} Згенеруйте новий у @BotFather."

    resolved, rerr = _resolve_recipient_chat_id(token, chat_id)
    if resolved is None:
        return False, rerr

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": resolved, "text": text},
            timeout=timeout,
        )
        data = r.json() if r.content else {}
        if r.status_code == 200 and data.get("ok"):
            return True, None
        desc = data.get("description") or r.text or r.reason
        return False, str(desc)[:500]
    except requests.RequestException as exc:
        return False, str(exc)


def notify_task_finished(task_label: str) -> str | None:
    import streamlit as st

    token = load_bot_token_from_secrets()
    default_chat = ""
    try:
        default_chat = str(st.secrets["telegram"].get("chat_id", "") or "").strip()
    except Exception:
        pass

    if not token:
        return None

    chat = (st.session_state.get("telegram_chat_id") or "").strip()
    if not chat:
        chat = default_chat

    if not chat:
        return None

    ok, err = send_telegram_message(token, chat, f"{task_label} — Finished")
    return err if not ok else None


def send_connect_test_message() -> tuple[bool, str | None]:
    import streamlit as st

    token = load_bot_token_from_secrets()
    chat = (st.session_state.get("telegram_chat_id") or "").strip()

    if not token:
        return (
            False,
            "Токен не знайдено. Локально: файл `.streamlit/secrets.toml` з секцією [telegram] і полем bot_token. "
            "На Streamlit Cloud: Settings → Secrets — той самий TOML. Після змін перезапустіть застосунок.",
        )
    if not chat:
        return False, "Введіть Chat ID або @username."

    return send_telegram_message(token, chat, CONNECT_TEST_MESSAGE)
