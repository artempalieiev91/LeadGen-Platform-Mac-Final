"""Запуск логіки з vendor/mathcurls/match_urls.py у тимчасовій директорії."""

from __future__ import annotations

import io
import sys
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from services.keep_awake import prevent_idle_sleep

_MATH_VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "mathcurls"
if str(_MATH_VENDOR) not in sys.path:
    sys.path.insert(0, str(_MATH_VENDOR))

from match_urls import MathcurlsStopped, step1_http, step2_browser  # noqa: E402


def run_mathcurls_pipeline(
    file_bytes: bytes,
    on_progress: Callable[[float, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    progress_holder: dict | None = None,
) -> tuple[bytes, str]:
    """
    on_progress(p, message) — p у [0, 1], викликається під час HTTP і Playwright кроків.
    progress_holder — опційно {"p": float, "msg": str}; оновлюється з того ж потоку, що й пайплайн.
    """
    import tempfile

    log = io.StringIO()

    def _sink(p: float, msg: str) -> None:
        if progress_holder is not None:
            progress_holder["p"] = min(max(p, 0.0), 1.0)
            progress_holder["msg"] = msg
        if on_progress is not None:
            on_progress(p, msg)

    def wrap_http(index: int, total: int) -> None:
        if total <= 0:
            return
        p = min((index / total) * 0.62, 0.62)
        _sink(p, f"HTTP-редиректи: {index} / {total}")

    def wrap_browser(index: int, total: int) -> None:
        if total <= 0:
            return
        p = 0.62 + min((index / total) * 0.38, 0.38)
        _sink(p, f"Playwright: {index} / {total}")

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        in_path = base / "input.csv"
        in_path.write_bytes(file_bytes)
        out_path = base / "output.csv"

        try:
            with prevent_idle_sleep():
                with redirect_stdout(log), redirect_stderr(log):
                    step1_http(
                        base=base,
                        input_filename="input.csv",
                        max_rows=None,
                        on_row=wrap_http,
                        should_stop=should_stop,
                    )
                    if should_stop and should_stop():
                        raise MathcurlsStopped()
                    _sink(0.62, "HTTP завершено, браузерна перевірка…")
                    step2_browser(base=base, on_row=wrap_browser, should_stop=should_stop)
                    _sink(1.0, "Готово")
        except MathcurlsStopped:
            partial_note = "\n\n[Зупинка] Збережено частковий output.csv (рядки до моменту зупинки)."
            if out_path.is_file():
                return out_path.read_bytes(), log.getvalue() + partial_note
            return b"", log.getvalue() + partial_note

        if not out_path.is_file():
            raise FileNotFoundError("Після обробки не знайдено output.csv")

        return out_path.read_bytes(), log.getvalue()
