"""
Після Sheets Preparation (крок 1+2): перевірка пари Website ↔ домен з email (логіка MathcURLs),
колонки Domain та Results (після Email): у Results — «Залишено» / «Видалено» з оригінальним статусом MathcURLs.
Усі рядки зберігаються у CSV.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from services.sheets_preparation_pipeline import parse_csv_bytes, rows_to_csv_bytes

_MATH_VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "mathcurls"
if str(_MATH_VENDOR) not in sys.path:
    sys.path.insert(0, str(_MATH_VENDOR))

from match_urls import (  # noqa: E402
    MathcurlsStopped,
    STATUS_DEAD,
    STATUS_NO_MATCH,
    ensure_scheme,
    match_pairs_batch,
)

# Для try/except у Streamlit без імпорту vendor напряму.
EmailDomainGateStopped = MathcurlsStopped

DROP_STATUSES = frozenset({STATUS_DEAD, STATUS_NO_MATCH})

COL_EMAIL = "Email"
COL_WEBSITE = "Website"
COL_DOMAIN = "Domain"
COL_RESULTS = "Results"


def _norm_h(s: str) -> str:
    return " ".join((s or "").strip().split()).casefold()


def _header_map(header: list[str]) -> dict[str, int]:
    m: dict[str, int] = {}
    for i, h in enumerate(header):
        k = _norm_h(str(h))
        if k not in m:
            m[k] = i
    return m


def domain_from_email(email: str) -> str | None:
    s = (email or "").strip()
    if not s or "@" not in s:
        return None
    dom = s.rsplit("@", 1)[-1].strip().lower()
    if not dom or "." not in dom:
        return None
    return dom


def _align_row(row: list[str], ncols: int) -> list[str]:
    r = list(row) + [""] * max(0, ncols - len(row))
    return r[:ncols]


def extend_header_for_gate(old_header: list[str]) -> tuple[list[str], bool, bool]:
    """
    Додає колонки Domain (одразу після Email) та Results (одразу після Domain), якщо їх ще немає.
    Повертає (новий_заголовок, створено_Domain, створено_Results).
    """
    hm = _header_map(old_header)
    ke, kd, kr = _norm_h(COL_EMAIL), _norm_h(COL_DOMAIN), _norm_h(COL_RESULTS)
    if ke not in hm:
        raise ValueError(f"У CSV немає колонки «{COL_EMAIL}».")
    ei = hm[ke]
    h = list(old_header)
    created_d, created_r = False, False
    if kd not in hm:
        h = h[: ei + 1] + [COL_DOMAIN] + h[ei + 1 :]
        created_d = True
        hm = _header_map(h)
    di = hm[kd]
    if kr not in hm:
        h = h[: di + 1] + [COL_RESULTS] + h[di + 1 :]
        created_r = True
    return h, created_d, created_r


def build_gate_row(
    old_header: list[str],
    old_row: list[str],
    new_header: list[str],
    domain_val: str,
    result_val: str,
) -> list[str]:
    hm_old = _header_map(old_header)
    pad = _align_row(old_row, len(old_header))
    out: list[str] = []
    for h in new_header:
        hn = _norm_h(str(h))
        if hn == _norm_h(COL_DOMAIN):
            out.append(domain_val)
        elif hn == _norm_h(COL_RESULTS):
            out.append(result_val)
        elif hn in hm_old:
            ix = hm_old[hn]
            out.append(pad[ix] if ix < len(pad) else "")
        else:
            out.append("")
    return out


def result_label_from_status(status: str) -> str:
    if status in DROP_STATUSES:
        return f"Видалено ({status})"
    return f"Залишено ({status})"


def results_that_clear_email_and_domain() -> frozenset[str]:
    """Значення колонки Results, для яких крок 4 очищає Email і Domain."""
    return frozenset(
        {
            result_label_from_status(STATUS_DEAD),
            result_label_from_status(STATUS_NO_MATCH),
        }
    )


def run_email_domain_gate(
    csv_bytes: bytes,
    *,
    on_progress: Callable[[float, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[list[list[str]], list[str]]:
    """
    Повертає (рядки CSV, журнал). Усі рядки даних зберігаються; колонки Domain та Results
    з результатом перевірки (для рядків без валідного домена в email — обидві порожні).
    """
    log: list[str] = []
    rows = parse_csv_bytes(csv_bytes)
    if not rows or len(rows) < 2:
        return rows, ["Немає даних для обробки."]

    old_header = rows[0]
    hm = _header_map(old_header)
    ke, kw = _norm_h(COL_EMAIL), _norm_h(COL_WEBSITE)
    if kw not in hm:
        raise ValueError(
            f"Потрібна колонка «{COL_WEBSITE}». Знайдено: {old_header!r}"
        )
    if ke not in hm:
        raise ValueError(
            f"Потрібна колонка «{COL_EMAIL}». Знайдено: {old_header!r}"
        )

    ei, wi = hm[ke], hm[kw]
    header_out, _cd, _cr = extend_header_for_gate(old_header)

    data_rows = rows[1:]
    pairs: list[tuple[str, str]] = []
    pair_source: list[int] = []

    for idx, row in enumerate(data_rows):
        pad = _align_row(row, len(old_header))
        em = pad[ei].strip() if ei < len(pad) else ""
        dom = domain_from_email(em)
        if dom is None:
            continue
        site = (pad[wi] if wi < len(pad) else "") or ""
        pairs.append((ensure_scheme(site.strip()), ensure_scheme(dom)))
        pair_source.append(idx)

    if not pairs:
        body: list[list[str]] = []
        for row in data_rows:
            body.append(build_gate_row(old_header, row, header_out, "", ""))
        log.append(
            "Немає рядків з валідним email-доменом — колонки Domain та Results додано (порожні)."
        )
        return [header_out, *body], log

    n_pairs = len(pairs)

    def _on_row(step: int, total: int) -> None:
        if on_progress is None or total <= 0:
            return
        p = min(step / total, 1.0)
        if step <= n_pairs:
            msg = f"HTTP-редиректи: {step} / {n_pairs}"
        else:
            k_rest = total - n_pairs
            msg = f"Браузер (Playwright): {step - n_pairs} / {k_rest}"
        on_progress(p, msg)

    try:
        statuses = match_pairs_batch(pairs, on_row=_on_row, should_stop=should_stop)
    except MathcurlsStopped:
        log.append("Зупинка до завершення перевірки.")
        raise

    n_dead = sum(1 for s in statuses if s == STATUS_DEAD)
    n_nomatch = sum(1 for s in statuses if s == STATUS_NO_MATCH)
    n_drop = sum(1 for s in statuses if s in DROP_STATUSES)
    n_keep = len(statuses) - n_drop
    log.append(
        f"Перевірено пар (email-домен): {len(pairs)}. "
        f"У колонці Results: Залишено — {n_keep}, Видалено — {n_drop} "
        f"(з них «{STATUS_DEAD}»: {n_dead}, «{STATUS_NO_MATCH}»: {n_nomatch})."
    )

    status_by_row: dict[int, str] = {
        pair_source[j]: statuses[j] for j in range(len(pair_source))
    }

    body_out: list[list[str]] = []
    for idx, row in enumerate(data_rows):
        pad = _align_row(row, len(old_header))
        dom = domain_from_email(pad[ei] if ei < len(pad) else "")
        if dom is None:
            body_out.append(build_gate_row(old_header, row, header_out, "", ""))
            continue
        st = status_by_row.get(idx, STATUS_DEAD)
        label = result_label_from_status(st)
        body_out.append(build_gate_row(old_header, row, header_out, dom, label))

    return [header_out, *body_out], log


def email_domain_gate_to_csv_bytes(
    csv_bytes: bytes,
    *,
    on_progress: Callable[[float, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[bytes, str]:
    rows, log_lines = run_email_domain_gate(
        csv_bytes, on_progress=on_progress, should_stop=should_stop
    )
    return rows_to_csv_bytes(rows), "\n".join(log_lines)
