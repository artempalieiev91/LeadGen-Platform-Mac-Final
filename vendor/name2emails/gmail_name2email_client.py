import argparse
import csv
import json
import math
import os
import random
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.error import URLError
from urllib.request import urlopen

from playwright.sync_api import Browser, BrowserContext, Page, TimeoutError, sync_playwright


INPUT_CSV = Path("Input.csv")
OUTPUT_CSV = Path("Output_With_Emails.csv")
# Значення в колонці Email, якщо Name2Email нічого не знайшов (не перезапускати повторно).
EMAIL_NOT_FOUND_PLACEHOLDER = "email not found"
CDP_URL = "http://127.0.0.1:9222"
CDP_VERSION_URL = f"{CDP_URL}/json/version"
GMAIL_URL = "https://mail.google.com/mail/u/0/#inbox"
AUTOSAVE_EVERY = 20
POLL_INTERVAL_MS = 200
POLL_MAX_MS = 45_000
STABLE_NOT_FOUND_STEPS = 4
TYPE_DELAY_MS = 50

# Recipient field (Gmail UA / EN); order: Ukrainian first, then To / textarea / name.
RECIPIENT_FIELD_SELECTORS = [
    'input[aria-label*="Кому"]',
    'input[aria-label*="To"]',
    'textarea[aria-label*="To"]',
    'input[name="to"]',
]

EMAILS_NOT_FOUND_PHRASE = "Emails were not found."

_EMAILS_NOT_FOUND_DIV_JS = r"""
(() => {
  const phrase = "Emails were not found.";
  const divs = document.querySelectorAll("div");
  for (let i = 0; i < divs.length; i++) {
    const t = divs[i].innerText || "";
    if (t.includes(phrase)) return true;
  }
  return false;
})()
"""

_NAME2EMAIL_SPINNER_JS = r"""
(() => {
  function isVisible(el) {
    if (!el || typeof el.getBoundingClientRect !== "function") return false;
    const st = window.getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden") return false;
    const op = parseFloat(st.opacity);
    if (!Number.isNaN(op) && op === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width >= 2 && r.height >= 2;
  }

  const spinSelectors = [
    "div.nametoemail-spinner",
    ".nametoemail-spinner",
    '[class*="nametoemail-spinner"]',
    '[class*="name2email-spinner"]',
  ];
  for (const sel of spinSelectors) {
    for (const el of document.querySelectorAll(sel)) {
      if (isVisible(el)) return true;
    }
  }

  const nameRoots = document.querySelectorAll(
    '[class*="nametoemail"], [class*="name2email"], [class*="Name2Email"]'
  );
  for (const root of nameRoots) {
    const cn = root.className != null ? String(root.className) : "";
    if (!/spinner|loader|-loading|is-loading|_loading/i.test(cn)) continue;
    if (isVisible(root)) return true;
  }

  let field = null;
  for (const sel of __RECIPIENT_SELECTORS__) {
    for (const el of document.querySelectorAll(sel)) {
      if (isVisible(el)) {
        field = el;
        break;
      }
    }
    if (field) break;
  }
  if (!field) return false;

  let root =
    field.closest('[role="dialog"]') ||
    field.closest("[data-compose-id]") ||
    field.closest("form");
  if (!root) root = document.body;

  const tr = field.getBoundingClientRect();
  for (const pb of root.querySelectorAll('[role="progressbar"]')) {
    if (!isVisible(pb)) continue;
    const pr = pb.getBoundingClientRect();
    if (pr.top >= tr.top - 4 && pr.top <= tr.bottom + 140) return true;
  }

  for (const svg of root.querySelectorAll("svg")) {
    if (!isVisible(svg)) continue;
    const sr = svg.getBoundingClientRect();
    const w = sr.width;
    const h = sr.height;
    if (w < 14 || w > 56 || h < 14 || h > 56) continue;
    if (!(sr.top >= tr.top - 4 && sr.top <= tr.bottom + 140)) continue;

    let strokeAnim = false;
    for (const c of svg.querySelectorAll("circle")) {
      if (
        c.hasAttribute("stroke-dasharray") &&
        c.hasAttribute("stroke")
      ) {
        strokeAnim = true;
        break;
      }
    }
    if (!strokeAnim) {
      for (const p of svg.querySelectorAll("path")) {
        if (
          p.hasAttribute("stroke-dasharray") &&
          p.hasAttribute("stroke")
        ) {
          strokeAnim = true;
          break;
        }
      }
    }
    if (strokeAnim) return true;
  }

  return false;
})()
"""
NAME2EMAIL_SPINNER_PAGE_JS = _NAME2EMAIL_SPINNER_JS.replace(
    "__RECIPIENT_SELECTORS__", json.dumps(RECIPIENT_FIELD_SELECTORS)
)
PAUSE_MIN_SECONDS = 1.0
PAUSE_MAX_SECONDS = 2.0
DEBUG_PORT = 9222
CHROME_CDP_WAIT_SECONDS = 20.0
CHROME_DEBUG_PROFILE = Path.home() / "chrome-debug-name2email"
CHROME_PATH_MAC = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
MAX_CLEAR_BACKSPACES = 30
EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _install_interrupt_signals(handler) -> None:
    """SIGINT/SIGTERM for CLI Ctrl+C. Streamlit / embedded runs often cannot install handlers — ignore any failure."""
    try:
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)
    except Exception:
        pass


class Name2EmailClient:
    def __init__(
        self,
        chrome_profile_dir: Optional[str] = None,
        wait_for_login: bool = True,
        on_progress: Optional[Callable[[int, int, str, str, str], None]] = None,
    ) -> None:
        self.work_rows: List[dict] = []
        self.fieldnames: List[str] = []
        self.email_col: str = ""
        self.q_col: str = ""
        self.processed_count = 0
        self.interrupted = False
        self.started_chrome_process: Optional[subprocess.Popen] = None
        self.chrome_profile_dir = Path(chrome_profile_dir).expanduser() if chrome_profile_dir else CHROME_DEBUG_PROFILE
        self.wait_for_login = wait_for_login
        self.output_path = OUTPUT_CSV
        self.on_progress = on_progress
        _install_interrupt_signals(self._handle_interrupt)

    def _emit_progress(self, current: int, total: int, query: str, email: str, status: str) -> None:
        if not self.on_progress:
            return
        try:
            self.on_progress(current, total, query, email, status)
        except Exception:
            pass

    def _handle_interrupt(self, _sig, _frame) -> None:
        self.interrupted = True
        print("\nCtrl+C detected. Saving progress before exit...")
        self._flush_pending_results()

    def run(self) -> None:
        if not INPUT_CSV.exists():
            raise FileNotFoundError(f"Input file not found: {INPUT_CSV}")

        try:
            self.work_rows, self.fieldnames, self.q_col, self.email_col = self._read_input_csv(INPUT_CSV)
        except ValueError as exc:
            print(f"Input.csv: {exc}")
            return

        if not self.work_rows:
            print("Input.csv has no data rows.")
            return

        merged = self._merge_output_into_rows(self.work_rows, self.fieldnames, OUTPUT_CSV, self.email_col)
        if not merged and OUTPUT_CSV.exists():
            print(
                f"Note: existing {OUTPUT_CSV.name} has a different shape than Input.csv; "
                "starting fresh (will overwrite on save)."
            )

        todo_indices = self._collect_todo_indices(self.work_rows, self.email_col, self.q_col)
        self.output_path = OUTPUT_CSV

        if not todo_indices:
            print(
                "Nothing to look up: every row either already has an email in the Email column, "
                f"already has «{EMAIL_NOT_FOUND_PLACEHOLDER}», or has no query in «Emails Research»."
            )
            self._write_merged_csv(OUTPUT_CSV, self.work_rows, self.fieldnames)
            return

        print(
            f"Loaded {len(self.work_rows)} data row(s); will run Name2Email for "
            f"{len(todo_indices)} row(s) with empty Email (others skipped)."
        )

        self._emit_progress(0, len(todo_indices), "", "", "init")
        try:
            with sync_playwright() as p:
                self._ensure_chrome_debugging_ready()
                browser = p.chromium.connect_over_cdp(CDP_URL)
                _context, page = self._prepare_gmail_page(browser)
                self._ensure_name2email_logged_in(page)
                compose_dialog = self._open_compose_and_get_to_input(page)

                for idx, row_i in enumerate(todo_indices):
                    if self.interrupted:
                        break

                    row = self.work_rows[row_i]
                    query = (row.get(self.q_col) or "").strip()
                    print(f"[{idx + 1}/{len(todo_indices)}] row {row_i + 2} Query: {query}")
                    email, status = self._process_single_query(page, compose_dialog, query)
                    self._emit_progress(idx + 1, len(todo_indices), query, email or "", status)

                    if status == "found" and email:
                        row[self.email_col] = email.strip()
                    elif status == "not_found":
                        row[self.email_col] = EMAIL_NOT_FOUND_PLACEHOLDER
                    # skipped: залишаємо Email порожнім

                    self.processed_count += 1

                    if self.processed_count % AUTOSAVE_EVERY == 0:
                        self._flush_pending_results()
                        print(f"Autosaved after {self.processed_count} processed rows.")

                    if not self.interrupted:
                        pause = random.uniform(PAUSE_MIN_SECONDS, PAUSE_MAX_SECONDS)
                        time.sleep(pause)
        finally:
            self._flush_pending_results()

        print("Done.")

    def _flush_pending_results(self) -> None:
        if not self.work_rows or not self.fieldnames:
            return
        self._write_merged_csv(self.output_path, self.work_rows, self.fieldnames)

    def _ensure_chrome_debugging_ready(self) -> None:
        if self._is_cdp_ready():
            return

        chrome_path = Path(CHROME_PATH_MAC)
        if not chrome_path.exists():
            raise RuntimeError(
                "Google Chrome is not found at default macOS path. "
                "Install Chrome or update CHROME_PATH_MAC in script."
            )

        self.chrome_profile_dir.mkdir(parents=True, exist_ok=True)
        command = [
            str(chrome_path),
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={self.chrome_profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        print("CDP is unavailable, starting Chrome with debug port automatically...")
        self.started_chrome_process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ.copy(),
        )

        deadline = time.monotonic() + CHROME_CDP_WAIT_SECONDS
        while time.monotonic() < deadline:
            if self._is_cdp_ready():
                print("Chrome debug port is ready.")
                return
            time.sleep(0.5)

        raise RuntimeError(
            f"Could not start Chrome with remote debugging on port {DEBUG_PORT} "
            f"within {CHROME_CDP_WAIT_SECONDS:.0f} seconds."
        )

    def _ensure_name2email_logged_in(self, page: Page) -> None:
        if not self.wait_for_login:
            return
        # Name2Email renders "Save contacts" in compose when authorized.
        # If it's absent, user likely needs to sign in once in this profile.
        marker_selectors = [
            "text=Save contacts",
            "text=Need to search in bulk?",
            "text=Зберегти контакти",
        ]
        for selector in marker_selectors:
            try:
                if page.locator(selector).count() > 0:
                    return
            except Exception:
                continue

        print(
            "Name2Email may not be logged in for this Chrome profile.\n"
            "Please sign in to Name2Email in the opened Chrome window, then press Enter here to continue..."
        )
        try:
            input()
        except EOFError:
            pass

    @staticmethod
    def _is_cdp_ready() -> bool:
        try:
            with urlopen(CDP_VERSION_URL, timeout=1.5) as resp:
                return resp.status == 200
        except (URLError, socket.timeout, OSError):
            return False

    def _prepare_gmail_page(self, browser: Browser) -> Tuple[BrowserContext, Page]:
        contexts = browser.contexts
        if not contexts:
            raise RuntimeError("No browser contexts found in the running Chrome instance.")

        gmail_page = None
        chosen_context = None
        for context in contexts:
            for page in context.pages:
                if "mail.google.com" in page.url.lower() or "gmail" in (page.title() or "").lower():
                    gmail_page = page
                    chosen_context = context
                    break
            if gmail_page:
                break

        if gmail_page and chosen_context:
            gmail_page.bring_to_front()
            return chosen_context, gmail_page

        chosen_context = contexts[0]
        gmail_page = chosen_context.new_page()
        gmail_page.goto(GMAIL_URL, wait_until="domcontentloaded")
        return chosen_context, gmail_page

    def _open_compose_and_get_to_input(self, page: Page):
        existing_dialog = page.locator("div[role='dialog']").last
        try:
            if existing_dialog.count() > 0 and existing_dialog.is_visible():
                self._find_to_input_in_dialog(page, existing_dialog)
                return existing_dialog
        except Exception:
            pass

        compose_selectors = [
            "div[gh='cm']",
            "text=Compose",
            "text=Написати",
            "div[role='button'][aria-label*='Compose']",
            "div[role='button'][aria-label*='Написати']",
        ]
        clicked = False
        for selector in compose_selectors:
            try:
                page.locator(selector).first.click(timeout=2500)
                clicked = True
                break
            except TimeoutError:
                continue
            except Exception:
                continue

        if not clicked:
            raise RuntimeError("Could not click Gmail Compose button.")

        dialog = page.locator("div[role='dialog']").last
        try:
            dialog.wait_for(timeout=5000, state="visible")
        except Exception:
            pass

        self._find_to_input_in_dialog(page, dialog)
        return dialog

    def _find_to_input_in_dialog(self, page: Page, dialog) -> None:
        """Focus first visible recipient control so compose is ready (broader fallbacks than per-row)."""
        to_selectors = list(RECIPIENT_FIELD_SELECTORS)
        to_selectors.extend(
            [
                "textarea[name='to']",
                "input[role='combobox']",
                "input[aria-label='To recipients']",
                "input[aria-label*='Одержувач']",
                "input[aria-label*='Получ']",
                "div[role='combobox'][aria-label*='To']",
                "div[role='combobox'][aria-label*='Кому']",
                "div[role='combobox'][aria-label*='Одержувач']",
                "div[contenteditable='true'][aria-label*='To']",
                "div[contenteditable='true'][aria-label*='Кому']",
                "div[contenteditable='true'][aria-label*='Одержувач']",
                "div[contenteditable='true'][aria-label*='Recipients']",
                "div[contenteditable='true'][role='combobox']",
            ]
        )
        scoped_selectors = [f"div[role='dialog'] {s}" for s in to_selectors]
        all_selectors = scoped_selectors + to_selectors
        for selector in all_selectors:
            locator = page.locator(selector)
            try:
                count = locator.count()
                for idx in range(count):
                    item = locator.nth(idx)
                    if item.is_visible():
                        item.click()
                        return
            except TimeoutError:
                continue
            except Exception:
                continue

        raise RuntimeError("Could not find 'To' input in compose window.")

    def _find_recipient_field(self, page: Page):
        dialog = page.locator("div[role='dialog']").last
        for scope in (dialog, page):
            try:
                if scope.count() == 0:
                    continue
            except Exception:
                continue
            for sel in RECIPIENT_FIELD_SELECTORS:
                try:
                    loc = scope.locator(sel)
                    for idx in range(loc.count()):
                        item = loc.nth(idx)
                        if item.is_visible():
                            return item
                except Exception:
                    continue
        return None

    def _process_single_query(self, page: Page, compose_dialog, query: str) -> Tuple[str, str]:
        to_input = self._find_recipient_field(page)
        if to_input is None:
            print("  Input field not found — skipping")
            return "", "skipped"

        self._clear_to_input(page, to_input, compose_dialog)
        to_input.click()
        page.keyboard.type(query, delay=TYPE_DELAY_MS)

        if " " in query:
            self._apply_space_trigger(page, query)

        email = self._poll_result_email(page, compose_dialog)
        if email:
            print(f"  -> found: {email}")
            return email, "found"

        print("  -> not found")
        return "", "not_found"

    def _clear_to_input(self, page: Page, to_input, compose_dialog) -> None:
        to_input.click()
        # Use cross-platform shortcut; Control+A is unreliable on macOS.
        page.keyboard.press("Meta+A")
        page.keyboard.press("Backspace")
        page.keyboard.press("Delete")
        page.wait_for_timeout(100)
        # Gmail may keep recipient pills, clear them with repeated backspaces.
        for _ in range(MAX_CLEAR_BACKSPACES):
            if not self._recipient_chip_present(compose_dialog):
                break
            page.keyboard.press("Backspace")
            page.wait_for_timeout(30)

    @staticmethod
    def _recipient_chip_present(container) -> bool:
        selectors = [
            "div.M9",
            "div.M9 span[email]",
            "span[email]",
        ]
        for selector in selectors:
            try:
                if container.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _apply_space_trigger(self, page: Page, query: str) -> None:
        first_space_index = query.find(" ")
        if first_space_index < 0:
            return

        page.keyboard.press("Home")
        for _ in range(first_space_index + 1):
            page.keyboard.press("ArrowRight")
        page.keyboard.press("Backspace")
        page.keyboard.type(" ", delay=TYPE_DELAY_MS)

    def _poll_result_email(self, page: Page, compose_dialog) -> Optional[str]:
        max_tries = math.ceil(POLL_MAX_MS / POLL_INTERVAL_MS)
        stable_not_found = 0

        for _ in range(max_tries):
            chip = self._extract_gmail_chip_email(compose_dialog)
            if chip:
                return chip

            if self._is_name2email_spinner_visible(page):
                stable_not_found = 0
                page.wait_for_timeout(POLL_INTERVAL_MS)
                continue

            if self._page_has_emails_not_found_in_any_div(page):
                stable_not_found += 1
                if stable_not_found >= STABLE_NOT_FOUND_STEPS:
                    break
            else:
                stable_not_found = 0

            page.wait_for_timeout(POLL_INTERVAL_MS)

        return self._extract_gmail_chip_email(compose_dialog)

    @staticmethod
    def _extract_gmail_chip_email(container) -> Optional[str]:
        try:
            loc = container.locator("div.M9 span[email]")
            for idx in range(loc.count()):
                item = loc.nth(idx)
                if not item.is_visible():
                    continue
                attr_email = (item.get_attribute("email") or "").strip()
                if attr_email and EMAIL_REGEX.fullmatch(attr_email):
                    return attr_email
        except Exception:
            pass
        return None

    def _is_name2email_spinner_visible(self, page: Page) -> bool:
        try:
            return bool(page.evaluate(NAME2EMAIL_SPINNER_PAGE_JS))
        except Exception:
            return False

    @staticmethod
    def _page_has_emails_not_found_in_any_div(page: Page) -> bool:
        try:
            return bool(page.evaluate(_EMAILS_NOT_FOUND_DIV_JS))
        except Exception:
            return False

    @staticmethod
    def _pick_query_column(fieldnames: List[str], email_col: Optional[str] = None) -> Optional[str]:
        """
        Стовпчик з пошуковим запитом Name2Email («Emails Research»).
        Не використовуємо «другий стовпець» у широких таблицях — там часто компанія/ім’я, не запит.
        """

        def norm(s: str) -> str:
            return re.sub(r"\s+", " ", (s or "").strip().lower())

        fn_orig = list(fieldnames)
        fn_norm = [norm(h) for h in fn_orig]

        # Як у services/name2emails_table_prepare._pick_column для Emails Research
        candidates = (
            "emails research",
            "emails_research",
            "research emails",
            "email research",
        )
        for cand in candidates:
            cn = norm(cand)
            for i, h in enumerate(fn_norm):
                if h == cn:
                    return fn_orig[i]
        for cand in candidates:
            cn = norm(cand)
            if len(cn) < 3:
                continue
            for i, h in enumerate(fn_norm):
                if h == cn or (cn in h and len(cn) >= 4):
                    return fn_orig[i]
        for fn in fieldnames:
            n = norm(fn)
            if "email" in n and "research" in n:
                return fn
        for fn in fieldnames:
            n = norm(fn)
            if n in ("name2email", "name to email", "n2e"):
                return fn
        # Типово після «Підготовка таблиці»: одразу після Email — Emails Research
        if email_col:
            try:
                ei = fn_orig.index(email_col)
            except ValueError:
                ei = -1
            if 0 <= ei < len(fn_orig) - 1:
                nxt = fn_orig[ei + 1]
                nn = norm(nxt)
                if "research" in nn or "name2email" in nn.replace(" ", ""):
                    return nxt
        # Лише два стовпці: старий формат (другий — запит)
        if len(fieldnames) == 2:
            return fieldnames[1]
        return None

    @staticmethod
    def _pick_email_column(fieldnames: List[str]) -> Optional[str]:
        """Колонка Email / Emails — не плутати з «Emails Research»."""
        cands = (
            "email",
            "emails",
            "e-mail",
            "e_mail",
            "email address",
            "електронна пошта",
            "імейл",
            "пошта",
        )
        fn_orig = list(fieldnames)
        fn_norm = [re.sub(r"\s+", " ", (h or "").strip().lower()) for h in fn_orig]
        for i, h in enumerate(fn_norm):
            if "research" in h:
                continue
            for cand in cands:
                cn = re.sub(r"\s+", " ", cand.strip().lower())
                if h == cn:
                    return fn_orig[i]
        for i, h in enumerate(fn_norm):
            if "research" in h:
                continue
            for cand in cands:
                cn = re.sub(r"\s+", " ", cand.strip().lower())
                if len(cn) >= 4 and (cn in h or h.startswith(cn)):
                    return fn_orig[i]
        return None

    def _read_input_csv(self, path: Path) -> Tuple[List[dict], List[str], str, str]:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            if not fieldnames:
                raise ValueError("no header or empty file")
            fn = list(fieldnames)
            email_col = Name2EmailClient._pick_email_column(fn)
            if not email_col:
                raise ValueError("could not find Email column")
            q_col = Name2EmailClient._pick_query_column(fn, email_col)
            if not q_col:
                raise ValueError(
                    "could not find «Emails Research» column — run «Підготовка таблиці» in the app, "
                    "or add a column named like «Emails Research» (not the 2nd column in a wide sheet)."
                )
            if q_col == email_col:
                raise ValueError("query column and Email column must be different")
            rows = [dict(row) for row in reader]
        return rows, fn, q_col, email_col

    @staticmethod
    def _merge_output_into_rows(
        rows: List[dict],
        fieldnames: List[str],
        out_path: Path,
        email_col: str,
    ) -> bool:
        """
        Якщо Output має ті самі колонки й кількість рядків, що Input — підтягуємо вже збережені Email
        (для відновлення після переривання).
        """
        if not out_path.exists():
            return False
        try:
            with out_path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                out_fn = reader.fieldnames
                if not out_fn or list(out_fn) != list(fieldnames):
                    return False
                out_rows = list(reader)
        except Exception:
            return False
        if len(out_rows) != len(rows):
            return False
        for i, orow in enumerate(out_rows):
            v = (orow.get(email_col) or "").strip()
            if v:
                rows[i][email_col] = v
        return True

    @staticmethod
    def _collect_todo_indices(rows: List[dict], email_col: str, q_col: str) -> List[int]:
        """Рядки, де треба запустити Name2Email: є запит у q_col і порожній Email."""
        todo: List[int] = []
        ph = EMAIL_NOT_FOUND_PLACEHOLDER.strip().lower()
        for i, row in enumerate(rows):
            q = (row.get(q_col) or "").strip()
            if not q or q.lower() in ("query", "research emails", "emails research"):
                continue
            em = (row.get(email_col) or "").strip()
            if not em:
                todo.append(i)
                continue
            if em.lower() == ph:
                continue
            # Уже є емейл або інший текст — не чіпаємо
        return todo

    @staticmethod
    def _write_merged_csv(path: Path, rows: List[dict], fieldnames: List[str]) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: (row.get(k) if row.get(k) is not None else "") for k in fieldnames})


def main() -> int:
    try:
        parser = argparse.ArgumentParser(description="Automated Gmail Name2Email client.")
        parser.add_argument(
            "--chrome-profile-dir",
            default=str(CHROME_DEBUG_PROFILE),
            help="Chrome user data directory used for auto-started debug browser.",
        )
        parser.add_argument(
            "--no-login-wait",
            action="store_true",
            help="Do not pause for manual Name2Email login confirmation.",
        )
        args = parser.parse_args()

        client = Name2EmailClient(
            chrome_profile_dir=args.chrome_profile_dir,
            wait_for_login=not args.no_login_wait,
        )
        client.run()
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
