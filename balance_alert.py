#!/usr/bin/env python3
"""
TeamApt Traft portal — automated pre-funded virtual balance alert.

Logs in with username/password (no 2FA), reads the "Pre-Funded Virtual
Balance" from /reports/transfers, and pushes an alert to Slack.

First run: set HEADFUL=1 in .env and run this script so you can SEE the page and
adjust the SELECTORS below to match the real DOM (right-click -> Inspect).
Once it works headful, set HEADFUL=0 and schedule it.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

URL = os.environ["TEAMAPT_URL"].rstrip("/")
USERNAME = os.environ["TEAMAPT_USERNAME"]
PASSWORD = os.environ["TEAMAPT_PASSWORD"]
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
THRESHOLD = os.environ.get("BALANCE_THRESHOLD", "").strip()
BALANCE_WARNING_THRESHOLD = 3_000_000_000  # 3 billion — amber alert threshold (edit here to change)
PAGERDUTY_ROUTING_KEY = os.environ.get("PAGERDUTY_ROUTING_KEY", "").strip()
HEADFUL = os.environ.get("HEADFUL", "0") == "1"

# Stable dedup key so repeated events map to the same PagerDuty incident.
PD_KEY_LOW = "teamapt-balance-low"
# Persistent latch across runs: remembers whether we've already paged for the
# CURRENT low episode, so we page once and don't re-page until the balance recovers.
# STATE_URL = an npoint.io bin URL (https://api.npoint.io/<id>) for ephemeral hosts
# that can't keep a local file (e.g. a GitHub Actions runner). Falls back to
# STATE_FILE for dev.
STATE_URL = os.environ.get("STATE_URL", "").strip()
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
# Auto-resolve the ticket this many seconds after paging (keeps MTTR clean).
# The latch stays set, so it won't re-page until the balance actually recovers.
AUTO_RESOLVE_SECONDS = int(os.environ.get("AUTO_RESOLVE_SECONDS", "300"))
# The job is triggered every 3 minutes by an external scheduler (cron-job.org,
# calling workflow_dispatch via the GitHub API — see README) so a LOW balance is
# caught within ~3 minutes, but the routine all-clear card would then spam the
# channel — so it only posts this often. Low, error, and recovery cards ignore
# this and always send.
OK_NOTIFY_INTERVAL_SECONDS = int(os.environ.get("OK_NOTIFY_INTERVAL_SECONDS", "900"))
# Absorbs scheduler jitter: a tick arriving a few seconds early shouldn't push the
# card to the next cycle, which would stretch the 15 min gap into 20.
OK_NOTIFY_GRACE_SECONDS = int(os.environ.get("OK_NOTIFY_GRACE_SECONDS", "90"))
# Warning (amber) cards are urgent-ish but not page-worthy, so they're throttled
# separately from the 15-min all-clear cadence — every ~5 min by default. Since the
# job itself still only scrapes every 3 min (unchanged), this doesn't touch the
# external cron-job.org schedule at all; the actual gap rounds up to the next 3-min
# tick (e.g. a 300s setting posts every 6 min, a 420s setting every 9 min) rather
# than landing exactly on the configured value.
WARNING_NOTIFY_INTERVAL_SECONDS = int(os.environ.get("WARNING_NOTIFY_INTERVAL_SECONDS", "300"))

# ---------------------------------------------------------------------------
# SELECTORS — confirmed against the real DOM (curled /login and /reports/transfers).
# ---------------------------------------------------------------------------
SEL_USERNAME = "#username"
SEL_PASSWORD = "#password"
SEL_LOGIN_BTN = "#login"
# On /reports/transfers — the span holding the "Pre-Funded Virtual Balance" figure.
SEL_BALANCE = "#LWalletBal"


# status -> (emoji, headline) for the alert card — mirrors CoralPay's format.
_STATUS = {
    # Real Unicode emoji, not ":shortcode:" text — shortcodes only render inside
    # Slack's rich-text view; OS push notifications and the "fallback" field show
    # them as literal text since there's no rich-text conversion there.
    "ok": ("\U0001F7E2", "Pre-Funded Virtual Balance"),           # 🟢
    "warning": ("\U0001F7E1", "Balance approaching low threshold"), # 🟡
    "low": ("\U0001F534", "LOW Pre-Funded Virtual Balance"),      # 🔴
    "error": ("\U0001F6A8", "Balance Check Failed"),              # 🚨
}


WAT = timezone(timedelta(hours=1))  # West Africa Time (UTC+1, no DST)


def _timestamp() -> str:
    return datetime.now(WAT).strftime("%Y-%m-%d %H:%M WAT")


def _body_lines(balance: str, threshold: str, detail: str) -> list:
    """The Current / Threshold / Site lines shown in the alert."""
    lines = [f"Current: {balance}"]
    if threshold:
        lines.append(f"Threshold: {threshold}")
    lines.append("Site: *TeamApt*")
    if detail:
        lines.append(f"Note: {detail}")
    return lines


def _slack_blocks(status: str, balance: str, threshold: str, detail: str) -> list:
    """Build a Block Kit alert card — same layout as CoralPay's: header, body
    section (Current/Threshold/Site/Note), then a context line with the
    checked-at timestamp. No attachments/color bar."""
    emoji, headline = _STATUS[status]
    body = "\n".join(_body_lines(balance, threshold, detail))
    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} {headline}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f":clock3: Checked {_timestamp()}"},
        ]},
    ]


def pagerduty(action: str, dedup_key: str, summary: str = "", severity: str = "warning",
              details: dict = None) -> None:
    """Send a PagerDuty Events API v2 event (trigger / resolve).

    'trigger' opens (or updates) an incident keyed by dedup_key; 'resolve'
    closes the incident with that same key. No-op if no routing key is set.
    """
    if not PAGERDUTY_ROUTING_KEY:
        return
    event = {
        "routing_key": PAGERDUTY_ROUTING_KEY,
        "event_action": action,
        "dedup_key": dedup_key,
    }
    if action == "trigger":
        payload = {
            "summary": summary[:1024],          # PD caps summary length
            "severity": severity,               # critical | error | warning | info
            "source": "traft.teamapt.com",
            "component": "prefunded-virtual-balance",
        }
        if details:
            payload["custom_details"] = details  # renders as a key/value panel in PD
        event["payload"] = payload
    r = requests.post("https://events.pagerduty.com/v2/enqueue", json=event, timeout=20)
    r.raise_for_status()
    print(f"[info] PagerDuty {action} ({dedup_key})")


def load_state() -> dict:
    """Read the 'already paged' latch. If STATE_URL is set (an npoint.io bin),
    read it over HTTP — needed because the runner's compute is ephemeral. Falls
    back to a local file for dev. Any read failure -> assume not paged; the
    PagerDuty dedup_key then prevents a duplicate incident, so this is safe."""
    if STATE_URL:
        try:
            r = requests.get(STATE_URL, timeout=20)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, dict) else {"paged": False}
        except Exception as e:  # noqa: BLE001
            print(f"[warn] could not read remote state ({e}); assuming not paged")
            return {"paged": False}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"paged": False}


def save_state(state: dict) -> None:
    """Persist the latch. POSTs the full JSON to the npoint.io bin when STATE_URL
    is set; otherwise writes the local file."""
    if STATE_URL:
        try:
            r = requests.post(STATE_URL, json=state, timeout=20)
            r.raise_for_status()
            print(f"[info] remote state updated: {state}")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] could not write remote state ({e}); latch may not persist")
        return
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def _card_due(state: dict, key: str, interval_seconds: int) -> bool:
    """True when a throttled card (tracked by `key`'s timestamp in the shared
    state bin) is due to post again, given `interval_seconds` and the shared
    scheduler-jitter grace (OK_NOTIFY_GRACE_SECONDS)."""
    last = state.get(key)
    if not isinstance(last, (int, float)):
        return True                 # never posted, or state predates this field
    elapsed = time.time() - last
    if elapsed < 0:
        return True                 # future timestamp (clock skew) — post and re-anchor
    return elapsed >= interval_seconds - OK_NOTIFY_GRACE_SECONDS


def ok_card_due(state: dict) -> bool:
    """True when the routine all-clear card is due to post.

    The job scrapes every 3 minutes (so a LOW balance is caught fast), but posting
    an 'ok' card on every run would flood the channel — so it posts only every
    OK_NOTIFY_INTERVAL_SECONDS, tracked by a timestamp in the shared state bin.
    """
    return _card_due(state, "last_ok_notified_at", OK_NOTIFY_INTERVAL_SECONDS)


def warning_card_due(state: dict) -> bool:
    """True when the amber warning card is due to post again (see
    WARNING_NOTIFY_INTERVAL_SECONDS) — same throttle pattern as ok_card_due,
    just a shorter interval since a worsening balance is more urgent."""
    return _card_due(state, "last_warning_notified_at", WARNING_NOTIFY_INTERVAL_SECONDS)


def notify(status: str, balance: str = "—", threshold: str = "", detail: str = "") -> None:
    """Send a formatted alert card to whichever channel is configured."""
    emoji, headline = _STATUS[status]
    # Slack rejects blocks whose text exceeds ~3000 chars (a full browser log/traceback
    # would 400). Keep the note short so error alerts actually send.
    detail = (detail or "").strip().replace("\n", " ")
    if len(detail) > 300:
        detail = detail[:300] + " …(truncated)"
    # Plain-text fallback for Slack notifications/previews.
    fallback = f"{emoji} {headline}\n" + "\n".join(_body_lines(balance, threshold, detail))
    fallback += f"\nChecked {_timestamp()}"

    if not SLACK_WEBHOOK_URL:
        print("[warn] No alert channel configured; message was:\n" + fallback)
        return
    # "text" alongside "blocks" is safe (Slack only uses "text" as the notification
    # fallback when "blocks" is present, so it doesn't render as a second stacked
    # message) — unlike "text" alongside "attachments", which does duplicate.
    r = requests.post(
        SLACK_WEBHOOK_URL,
        json={"text": fallback, "blocks": _slack_blocks(status, balance, threshold, detail)},
        timeout=20,
    )
    if not r.ok:
        # Don't use r.raise_for_status(): its message embeds the full request URL,
        # which for a Slack webhook IS the secret — that would leak it into logs.
        raise RuntimeError(f"Slack webhook rejected the message: HTTP {r.status_code} {r.text!r}")


def parse_amount(raw: str):
    """Pull a numeric amount out of a string like '2,906,639,728.14'."""
    m = re.search(r"[-+]?[\d,]*\.?\d+", raw.replace(",", ""))
    return float(m.group()) if m else None


def _dump_debug(page) -> None:
    """On failure, log where we ended up. No screenshot/HTML capture: on ephemeral
    CI compute those files don't persist, and these log lines (captured in the
    GitHub Actions job log) are the useful diagnostic — e.g. a login URL means
    login failed."""
    try:
        print(f"[debug] final url:   {page.url}")
        print(f"[debug] page title:  {page.title()}")
    except Exception as e:  # noqa: BLE001
        print(f"[debug] could not read page state: {e}")


def fetch_balance() -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not HEADFUL)
        ctx = browser.new_context()
        page = ctx.new_page()
        try:
            # 1) Login page — username/password only, no 2FA step. The page's own
            # onclick handler RSA-encrypts the password field before the form posts
            # to /login; that runs automatically in-browser on click, nothing to do here.
            # "networkidle" is avoided throughout: prod has background polling (live
            # balance/dashboard widgets) that never lets the network go fully quiet,
            # so it times out intermittently. "domcontentloaded" plus the explicit
            # wait_for_selector/wait_for_function below is enough — Playwright's
            # fill/click already auto-wait for elements to be ready regardless.
            page.goto(f"{URL}/login", wait_until="domcontentloaded")
            page.fill(SEL_USERNAME, USERNAME)
            page.fill(SEL_PASSWORD, PASSWORD)
            page.click(SEL_LOGIN_BTN)
            page.wait_for_load_state("domcontentloaded")

            # 2) Transfers report page
            page.goto(f"{URL}/reports/transfers", wait_until="domcontentloaded")
            page.wait_for_selector(SEL_BALANCE, timeout=20000)

            # The balance loads via AJAX AFTER the element renders, so it briefly
            # shows a placeholder (0.00 / empty). Wait until it shows a real,
            # non-zero amount. If the account is genuinely 0 this times out and we
            # read it as-is — so a true zero still works, just a few seconds slower.
            try:
                page.wait_for_function(
                    """() => {
                        const el = document.getElementById('LWalletBal');
                        if (!el) return false;
                        const n = parseFloat(el.textContent.replace(/[^0-9.]/g, ''));
                        return !isNaN(n) && n > 0;
                    }""",
                    timeout=15000,
                )
            except PWTimeout:
                print("[warn] balance still 0/empty after wait; reading as-is")

            text = page.locator(SEL_BALANCE).first.inner_text()
            # Normalise: drop &nbsp;, collapse whitespace.
            text = text.replace("\xa0", " ")
            text = re.sub(r"\s+", " ", text).strip()
            return text
        except Exception:
            _dump_debug(page)
            raise
        finally:
            browser.close()


def main() -> int:
    try:
        raw = fetch_balance()
    except Exception as e:  # noqa: BLE001
        notify("error", detail=str(e))
        print(f"[error] {e}", file=sys.stderr)
        return 1

    amount = parse_amount(raw)
    print(f"[info] Balance read: {raw!r} -> {amount}")
    # #LWalletBal's text is just digits/commas, no currency symbol — add it for display.
    balance_display = f"₦{raw}"

    # Check balance against thresholds:
    # - THRESHOLD = critical/low (page)
    # - BALANCE_WARNING_THRESHOLD (3B) = warning threshold (amber card, no page)
    limit = None
    threshold_display = ""
    if THRESHOLD:
        try:
            limit = float(THRESHOLD)
            threshold_display = f"₦{limit:,.2f}"
        except ValueError:
            print(f"[warn] BALANCE_THRESHOLD {THRESHOLD!r} is not a number; ignoring.")

    is_low = limit is not None and amount is not None and amount < limit
    # Warning: balance is between the critical threshold and 3B.
    # Only show warning state if balance is below 3B but above the threshold.
    is_warning = (amount is not None and amount < BALANCE_WARNING_THRESHOLD and
                  (limit is None or amount >= limit) and not is_low)

    # Edge-triggered PagerDuty: page once when we cross into low, then stay latched
    # until the balance recovers (funded). The latch survives across runs via STATE_URL/STATE_FILE.
    state = load_state()
    already_paged = state.get("paged", False)

    # State writes MERGE into the loaded dict (never replace it) so the paged latch
    # and the all-clear throttle timestamp don't clobber each other.
    if is_low:
        # A low balance is urgent: card every run, no throttle.
        notify("low", balance=balance_display, threshold=threshold_display)
        if not already_paged:
            summary = f"TeamApt pre-funded balance LOW — Current: {balance_display}, Threshold: {threshold_display}, Site: TeamApt"
            details = {
                "current_balance": balance_display,
                "threshold": threshold_display or "not set",
                "site": "TeamApt",
                "balance_url": f"{URL}/reports/transfers",
                "checked_at": _timestamp(),
            }
            pagerduty("trigger", PD_KEY_LOW, summary, "critical", details=details)
            # Latch BEFORE the wait so we never re-page even if the run is interrupted.
            state["paged"] = True
            save_state(state)
            print("[info] Low balance: opened PagerDuty incident and latched.")
            if AUTO_RESOLVE_SECONDS > 0:
                print(f"[info] Waiting {AUTO_RESOLVE_SECONDS}s, then auto-resolving the ticket.")
                time.sleep(AUTO_RESOLVE_SECONDS)
                pagerduty("resolve", PD_KEY_LOW)
                print("[info] Auto-resolved ticket; latch stays set until balance recovers.")
        else:
            print("[info] Low balance: already paged this episode; not re-paging.")
    elif is_warning:
        # Warning (amber): balance between threshold and 3B. No PagerDuty page. The
        # card itself is throttled to WARNING_NOTIFY_INTERVAL_SECONDS (default ~5 min)
        # rather than posting every 3-min run — recovery-from-critical still bypasses
        # the throttle since that's news worth seeing immediately.
        dirty = False
        if warning_card_due(state) or already_paged:
            notify("warning", balance=balance_display, threshold=threshold_display)
            state["last_warning_notified_at"] = int(time.time())
            dirty = True
        else:
            waited = int(time.time() - state.get("last_warning_notified_at", 0))
            print(f"[info] Balance warning; card throttled ({waited}s since last, "
                  f"posts every {WARNING_NOTIFY_INTERVAL_SECONDS}s).")
        if already_paged:
            # Balance recovered from critical -> just warning
            pagerduty("resolve", PD_KEY_LOW)
            state["paged"] = False
            dirty = True
            print("[info] Balance in warning range (above threshold but below 3B); resolved PagerDuty incident.")
        if dirty:
            save_state(state)
    else:
        # Recovery is news worth hearing immediately, so it bypasses the throttle.
        dirty = False
        if ok_card_due(state) or already_paged:
            notify("ok", balance=balance_display)
            state["last_ok_notified_at"] = int(time.time())
            dirty = True
        else:
            waited = int(time.time() - state.get("last_ok_notified_at", 0))
            print(f"[info] Balance OK; card throttled ({waited}s since last, "
                  f"posts every {OK_NOTIFY_INTERVAL_SECONDS}s).")
        if already_paged:
            pagerduty("resolve", PD_KEY_LOW)   # idempotent if already auto-resolved
            state["paged"] = False
            dirty = True
            print("[info] Balance recovered: resolved PagerDuty incident and reset latch.")
        if dirty:
            save_state(state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
