"""Під час довгих задач утримує систему від переходу в idle sleep (macOS: caffeinate)."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def prevent_idle_sleep() -> Iterator[None]:
    """
    На macOS запускає ``caffeinate -dims`` на час блоку (екран і простій без сну).
    Якщо ноутбук повністю засинає (кришка, ручний Sleep) — це не завжди можна обійти.
    """
    if sys.platform != "darwin":
        yield
        return
    try:
        proc = subprocess.Popen(
            ["caffeinate", "-dims"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        yield
        return
    try:
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
