"""Локальний запуск Streamlit (зручно для PyCharm / Run)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def main() -> None:
    cmd = [sys.executable, "-m", "streamlit", "run", "streamlit_app.py"]
    raise SystemExit(subprocess.call(cmd, cwd=_ROOT))


if __name__ == "__main__":
    main()
