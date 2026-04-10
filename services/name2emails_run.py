"""Запуск gmail_name2email_client з vendor/name2emails (локальний Chrome + Gmail)."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

_VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "name2emails"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))


def name2emails_supported_platform() -> bool:
    return sys.platform == "darwin"


def run_name2email_client(
    file_bytes: bytes,
    *,
    chrome_profile_dir: str | None = None,
    wait_for_login: bool = False,
    on_progress: Callable[[int, int, str, str, str], None] | None = None,
) -> tuple[bytes, str]:
    """
    Записує завантажений CSV як Input.csv у тимчасову папку, змінює cwd і викликає Name2EmailClient.
    wait_for_login=False для Streamlit (без input() у терміналі).
    on_progress(current, total, query, email, status) — після кожного рядка; current=0, status='init' перед циклом.
    Повертає (bytes Output_With_Emails.csv — той самий CSV що Input, з оновленою колонкою Email, або порожній якщо файл не створено, текст логу).
    """
    import io
    from contextlib import redirect_stderr, redirect_stdout
    import tempfile

    if not name2emails_supported_platform():
        raise RuntimeError(
            "Автозапуск Chrome з репозиторію розрахований на macOS. "
            "Запускайте цю вкладку локально на Mac з установленим Google Chrome."
        )

    log = io.StringIO()
    from gmail_name2email_client import Name2EmailClient  # noqa: E402

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "Input.csv").write_bytes(file_bytes)

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            with redirect_stdout(log), redirect_stderr(log):
                client = Name2EmailClient(
                    chrome_profile_dir=chrome_profile_dir,
                    wait_for_login=wait_for_login,
                    on_progress=on_progress,
                )
                client.run()
        finally:
            os.chdir(old_cwd)

        out = tmp_path / "Output_With_Emails.csv"
        if not out.is_file():
            return b"", log.getvalue()
        return out.read_bytes(), log.getvalue()
