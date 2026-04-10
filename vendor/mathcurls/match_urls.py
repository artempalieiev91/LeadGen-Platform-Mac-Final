#!/usr/bin/env python3
"""
Match URL pairs from input.csv: HTTP redirects (requests), then browser redirects (Playwright).

Один запуск виконує обидва кроки підряд (HTTP, потім Playwright, якщо є пари для браузера).

  python3 match_urls.py

Опційно:  python3 match_urls.py --input my.csv   або   python3 match_urls.py --limit 50

Перед першим запуском (macOS):
  python3 -m pip install -r requirements.txt
  python3 -m playwright install chromium
"""

from __future__ import annotations

import argparse
import csv
import os
import platform
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path


class MathcurlsStopped(Exception):
    """Кооперативна зупинка між рядками (наприклад, з інтерфейсу платформи)."""


def _check_stop(should_stop: Callable[[], bool] | None) -> None:
    if should_stop and should_stop():
        raise MathcurlsStopped()
from urllib.parse import urlparse

import requests

INPUT_FILE = "input.csv"
OUTPUT_FILE = "output.csv"
TMP_FILE = "puppeteer_needed.tmp.csv"


def _chrome_user_agent_macos() -> str:
    """User-Agent як у Chrome на macOS (Intel або Apple Silicon)."""
    if platform.system() != "Darwin":
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    rel = platform.mac_ver()[0] or "10.15.7"
    mac_ver = rel.replace(".", "_")
    arch = "arm64" if platform.machine() == "arm64" else "Intel"
    return (
        f"Mozilla/5.0 (Macintosh; {arch} Mac OS X {mac_ver}) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )


USER_AGENT_HTTP = _chrome_user_agent_macos()
USER_AGENT_BROWSER = _chrome_user_agent_macos()


def normalize_url(raw_url: str) -> str:
    raw_url = raw_url.strip()
    if not raw_url:
        return ""
    try:
        if not raw_url.startswith(("http://", "https://")):
            raw_url = "https://" + raw_url
        parsed = urlparse(raw_url)
        if not parsed.netloc:
            return ""
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        path = (parsed.path or "").rstrip("/").lower()
        return host + path
    except Exception:
        return ""


def ensure_scheme(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return "https://" + url
    return url


def get_final_curl_url(url: str) -> str:
    """Follow redirects with HEAD; fallback to GET if HEAD fails. Mirrors PHP curl behavior."""
    url = ensure_scheme(url)
    session = requests.Session()
    headers = {"User-Agent": USER_AGENT_HTTP}
    try:
        r = session.head(url, allow_redirects=True, timeout=15, headers=headers)
        if r.status_code == 405:
            r = session.get(url, allow_redirects=True, timeout=15, headers=headers, stream=True)
            r.close()
        return r.url
    except requests.exceptions.Timeout:
        return "TIMEOUT_CURL"
    except requests.exceptions.RequestException:
        return ""
    finally:
        session.close()


def progress_bar(index: int, total: int, width: int = 30) -> str:
    if total <= 0:
        return f"[{'-' * width}] 0% (0/0)"
    percent = (index * 100) // total
    done = (percent * width) // 100
    left = width - done
    return f"[{'#' * done}{'-' * left}] {percent}% ({index}/{total})"


def step1_http(
    *,
    base: Path,
    input_filename: str,
    max_rows: int | None,
    on_row: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    print("=== Step 1: HTTP redirect checking (requests) ===")
    input_path = base / input_filename
    output_path = base / OUTPUT_FILE
    tmp_path = base / TMP_FILE

    if not input_path.is_file():
        print(f"Missing {input_path.name}", file=sys.stderr)
        sys.exit(1)

    with open(input_path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    rows = [r for r in rows if r and any((c or "").strip() for c in r[:2])]
    if max_rows is not None:
        rows = rows[:max_rows]
        print(f"Обмеження: перші {len(rows)} рядків з {input_filename}")
    total = len(rows)

    with (
        open(output_path, "w", newline="", encoding="utf-8") as out_f,
        open(tmp_path, "w", newline="", encoding="utf-8") as tmp_f,
    ):
        writer = csv.writer(out_f)
        tmp_writer = csv.writer(tmp_f)

        for index, data in enumerate(rows, start=1):
            _check_stop(should_stop)
            bar = progress_bar(index, total)
            sys.stdout.write(f"\r{bar}{' ' * 10}")
            sys.stdout.flush()
            if on_row is not None:
                on_row(index, total)

            if len(data) < 2:
                continue
            source_url = (data[0] or "").strip()
            second_url = (data[1] or "").strip()

            if source_url == second_url:
                writer.writerow([source_url, second_url, "Matched"])
                continue

            final1 = get_final_curl_url(source_url)
            final2 = get_final_curl_url(second_url)

            if final1 == "TIMEOUT_CURL" or final2 == "TIMEOUT_CURL":
                writer.writerow([source_url, second_url, "One of websites is dead"])
                tmp_writer.writerow([source_url, second_url])
                continue
            if not final1 or not final2:
                writer.writerow([source_url, second_url, "One of websites is dead"])
                tmp_writer.writerow([source_url, second_url])
                continue

            if normalize_url(final1) == normalize_url(final2):
                writer.writerow([source_url, second_url, "Matched with redirect"])
            else:
                tmp_writer.writerow([source_url, second_url])

    print("\n")
    print("Step 1 complete.")


def _playwright_browser_dir() -> Path:
    """Каталог у корені репозиторію — не залежить від тимчасових шляхів sandbox (наприклад Cursor)."""
    return Path(__file__).resolve().parents[2] / ".playwright-browsers"


def _configure_playwright_browser_dir() -> Path:
    """Фіксує PLAYWRIGHT_BROWSERS_PATH до проєкту, щоб install/launch шукали одне й те саме місце."""
    bdir = _playwright_browser_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bdir)
    return bdir


def _launch_chromium_or_install(p) -> object:
    """
    Запуск Chromium; якщо бінарника немає — один раз виконує
    ``python -m playwright install chromium`` і повторює launch.
    """
    try:
        return p.chromium.launch(headless=True)
    except Exception as e:
        err = str(e).lower()
        missing = (
            "executable doesn't exist" in err
            or "playwright install" in err
            or ("doesn't exist" in err and "launch" in err)
        )
        if not missing:
            raise
        print(
            "Chromium для Playwright не знайдено — завантаження (може зайняти хвилину)…",
            file=sys.stderr,
        )
        r = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            out = ((r.stderr or "") + "\n" + (r.stdout or "")).strip()
            raise RuntimeError(
                "Не вдалося автоматично встановити Chromium для Playwright.\n"
                "Виконайте вручну в тому ж середовищі Python, що й Streamlit:\n\n"
                f"    {sys.executable} -m playwright install chromium\n\n"
                f"Вивід команди:\n{out[:4000]}"
            ) from e
        try:
            return p.chromium.launch(headless=True)
        except Exception as e2:
            raise RuntimeError(
                "Після встановлення Chromium запуск браузера все ще не вдався. "
                "Перезапустіть Streamlit і перевірте, що команда "
                f"`{sys.executable} -m playwright install chromium` завершується без помилок."
            ) from e2


# Рядки статусу збігаються з output.csv (step1/step2) — для фільтрації в Sheets Preparation.
STATUS_MATCHED = "Matched"
STATUS_HTTP_REDIRECT = "Matched with redirect"
STATUS_DEAD = "One of websites is dead"
STATUS_PLAYWRIGHT = "Match with redirect with Playwright"
STATUS_NO_MATCH = "No Redirect or not Match"


def _browser_get_final_url(
    page,
    input_url: str,
    *,
    log_failures: bool = False,
) -> str | None:
    """
    Фінальний URL після навігації. «load» надійніший за «domcontentloaded» для ланцюгів редиректів;
    коротка пауза допомагає, коли редирект завершується скриптом після події load.
    """
    full_url = input_url.strip()
    if not full_url.startswith(("http://", "https://")):
        full_url = "https://" + full_url
    try:
        page.goto(full_url, wait_until="load", timeout=30000)
        page.wait_for_timeout(500)
        return page.url
    except Exception as e:
        if log_failures:
            print(f"\nWarning: Error opening {input_url}: {e}")
        return None


def match_pairs_batch(
    pairs: list[tuple[str, str]],
    *,
    on_row: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[str]:
    """
    Та сама логіка, що step1_http + step2_browser, але порядок результатів = порядок `pairs`.
    Кожен елемент — (url_з_таблиці, url_для_домену_email), обидва як у MathcURLs.
    """
    n = len(pairs)
    out: list[str | None] = [None] * n
    need_browser: list[tuple[int, str, str]] = []
    # Під час HTTP оцінюємо верхню межу як n + n (якщо всі пари підуть у браузер);
    # після HTTP реальна межа = n + len(need_browser). Раніше було n*2 у підписі — виглядало як «подвоєна» кількість.
    http_total_est = max(n * 2, 1)

    for i, (a, b) in enumerate(pairs):
        _check_stop(should_stop)
        if on_row is not None:
            on_row(i + 1, http_total_est)
        u1, u2 = (a or "").strip(), (b or "").strip()
        if u1 == u2:
            out[i] = STATUS_MATCHED
            continue
        f1 = get_final_curl_url(u1)
        f2 = get_final_curl_url(u2)
        http_dead = (
            f1 == "TIMEOUT_CURL"
            or f2 == "TIMEOUT_CURL"
            or not f1
            or not f2
        )
        if not http_dead and normalize_url(f1) == normalize_url(f2):
            out[i] = STATUS_HTTP_REDIRECT
            continue
        need_browser.append((i, u1, u2))

    k = len(need_browser)
    if not k:
        if on_row is not None and n > 0:
            on_row(n, n)
        return [x or STATUS_DEAD for x in out]

    total_steps = n + k
    _configure_playwright_browser_dir()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = _launch_chromium_or_install(p)
        try:
            for j, (i, u1, u2) in enumerate(need_browser, start=1):
                _check_stop(should_stop)
                if on_row is not None:
                    on_row(n + j, total_steps)
                page = browser.new_page(user_agent=USER_AGENT_BROWSER)
                try:
                    final1 = _browser_get_final_url(page, u1)
                finally:
                    page.close()
                page = browser.new_page(user_agent=USER_AGENT_BROWSER)
                try:
                    final2 = _browser_get_final_url(page, u2)
                finally:
                    page.close()

                if not final1 or not final2:
                    out[i] = STATUS_DEAD
                elif normalize_url(final1) == normalize_url(final2):
                    out[i] = STATUS_PLAYWRIGHT
                else:
                    out[i] = STATUS_NO_MATCH
        finally:
            browser.close()

    return [x or STATUS_DEAD for x in out]


def step2_browser(
    *,
    base: Path,
    on_row: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    _configure_playwright_browser_dir()
    from playwright.sync_api import sync_playwright

    tmp_path = base / TMP_FILE
    output_path = base / OUTPUT_FILE

    if not tmp_path.is_file() or tmp_path.stat().st_size == 0:
        print("No pairs in puppeteer_needed.tmp.csv — skipping Playwright step.")
        return

    _check_stop(should_stop)

    with open(tmp_path, newline="", encoding="utf-8") as f:
        records = [r for r in csv.reader(f) if len(r) >= 2 and (r[0].strip() or r[1].strip())]

    if not records:
        print("puppeteer_needed.tmp.csv is empty — skipping Playwright step.")
        return

    print("=== Step 2: Playwright browser redirect checking ===")
    print(f"Checking {len(records)} pair(s)...")

    with sync_playwright() as p:
        browser = _launch_chromium_or_install(p)
        try:
            with open(output_path, "a", newline="", encoding="utf-8") as out_f:
                writer = csv.writer(out_f)
                total = len(records)
                for index, row in enumerate(records, start=1):
                    _check_stop(should_stop)
                    url1, url2 = row[0].strip(), row[1].strip()
                    bar = progress_bar(index, total)
                    sys.stdout.write(f"\r{bar} Checking: {url1}")
                    sys.stdout.flush()
                    if on_row is not None:
                        on_row(index, total)

                    page = browser.new_page(user_agent=USER_AGENT_BROWSER)
                    try:
                        final1 = _browser_get_final_url(page, url1, log_failures=True)
                    finally:
                        page.close()

                    page = browser.new_page(user_agent=USER_AGENT_BROWSER)
                    try:
                        final2 = _browser_get_final_url(page, url2, log_failures=True)
                    finally:
                        page.close()

                    if not final1 or not final2:
                        print(f"\nOne of websites is dead: {url1}, {url2}")
                        writer.writerow([url1, url2, "One of websites is dead"])
                        continue

                    if normalize_url(final1) == normalize_url(final2):
                        print(f"\nMatch with redirect (Playwright): {url1} -> {final1}")
                        writer.writerow([url1, url2, "Match with redirect with Playwright"])
                    else:
                        print(f"\nNo match: {url1} -> {final1} | {url2} -> {final2}")
                        writer.writerow([url1, url2, "No Redirect or not Match"])
        finally:
            browser.close()

    print("\nPlaywright step completed.")
    print(f"Done. Results in {OUTPUT_FILE}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Порівняння пар URL з CSV (HTTP + Playwright).")
    p.add_argument(
        "--input",
        "-i",
        default=INPUT_FILE,
        metavar="FILE",
        help=f"Вхідний CSV (два стовпчики A,B). За замовчуванням: {INPUT_FILE}",
    )
    p.add_argument(
        "--limit",
        "-n",
        type=int,
        default=None,
        metavar="N",
        help="Обробити лише перші N рядків (швидкий тест на великому файлі).",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    base = Path(__file__).resolve().parent
    step1_http(base=base, input_filename=args.input, max_rows=args.limit)
    step2_browser(base=base)


if __name__ == "__main__":
    main()
