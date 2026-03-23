#!/usr/bin/env python3
"""
Desktop Context — standalone terminal daemon.
Watches iMessage, WhatsApp, Chrome tabs, clipboard, calendar, and screen
for signals that relate to your active goals, then fires macOS notifications
when confidence is high.

Usage:
    source venv/bin/activate
    ANTHROPIC_API_KEY=... python3 monitor_terminal.py
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta

from anthropic import Anthropic
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ── Paths ────────────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
GOALS_MD = os.path.join(DATA_DIR, "goals.md")
DESKTOP_MEMORY_MD = os.path.join(DATA_DIR, "desktop_memory.md")
PREDICTED_ACTIONS_MD = os.path.join(DATA_DIR, "predicted_actions.md")
AMBIENT_DB = os.path.join(BASE_DIR, "internal", "ambient.db")

# ── Configuration ────────────────────────────────────────────────────────────

SIGNAL_INTERVAL = 2        # seconds between signal collection
ANALYSIS_INTERVAL = 15     # seconds between LLM analysis
SCREEN_INTERVAL = 10       # seconds between screenshot analysis
ANALYSIS_MODEL = "claude-haiku-4-5-20251001"

GOAL_COLORS = ["bright_cyan", "bright_green", "bright_yellow", "bright_magenta",
               "bright_red", "bright_blue", "deep_sky_blue1", "spring_green1"]

IGNORED_APPS = {"cmux", "Activity Monitor", "Python"}
# Only capture screenshots for these apps (avoid self-referential terminal captures)
SCREENSHOT_APPS = {"Google Chrome", "Safari", "Arc", "Brave Browser", "Firefox",
                   "Messages", "WhatsApp", "Mail", "Superhuman", "Calendar",
                   "Preview", "Finder", "Slack", "Notes", "Maps"}

# ── Globals ──────────────────────────────────────────────────────────────────

console = Console()
lock = threading.Lock()

# Goal / people context — loaded at startup
goals_config: dict = {}

# Per-goal accumulated signals: {goal_name: [signal_dict, ...]}
goal_signals: dict = {}

# Deduplication sets
seen_imessage_ids: set = set()   # (sender, timestamp, text_hash)
seen_whatsapp_ids: set = set()
seen_tab_urls: set = set()
seen_clipboard_hash: str = ""

# Rolling log lines for display (newest first, capped at 200)
log_lines: list = []

# Browser & app state (for display panels)
current_tabs: list = []       # [{title, url}, ...]
current_app: str = ""         # frontmost app name
current_window_title: str = ""  # frontmost window title
last_tab_event: str = ""      # most recent tab change
last_app_event: str = ""      # most recent app switch
running_apps: list = []       # foreground apps

# Screen analysis state
last_screen_summary: str = "" # latest vision description
# Anthropic client — initialised in main()
client: Anthropic = None




# ── API key helper ───────────────────────────────────────────────────────────

def load_env():
    """Load .env file if present."""
    env_file = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())


def get_api_key() -> str:
    """Return ANTHROPIC_API_KEY from .env or environment."""
    load_env()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    console.print("[bold red]No ANTHROPIC_API_KEY found. Add it to .env file.[/bold red]")
    sys.exit(1)


# ── Signal collectors (adapted from app.py) ──────────────────────────────────

def get_recent_imessages(minutes: int = 5, limit: int = 30) -> list[dict]:
    """Return list of {sender, text, dt, id_key} dicts from iMessage."""
    results = []
    try:
        db_path = os.path.expanduser("~/Library/Messages/chat.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cutoff_unix = (datetime.now() - timedelta(minutes=minutes)).timestamp()
        cutoff_apple_ns = int((cutoff_unix - 978307200) * 1_000_000_000)
        rows = conn.execute("""
            SELECT
                m.ROWID,
                m.text,
                datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime') as dt,
                CASE WHEN m.is_from_me = 1 THEN 'me' ELSE
                    COALESCE(h.id, 'unknown')
                END as sender
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.text IS NOT NULL AND m.text != '' AND m.date > ?
            ORDER BY m.date DESC
            LIMIT ?
        """, (cutoff_apple_ns, limit)).fetchall()
        conn.close()
        for r in rows:
            sender = "You" if r["sender"] == "me" else r["sender"]
            text = (r["text"] or "")[:300]
            id_key = (sender, r["dt"], hashlib.md5(text.encode()).hexdigest()[:12])
            results.append({"sender": sender, "text": text, "dt": r["dt"],
                            "id_key": id_key, "source": "imessage"})
    except Exception as e:
        add_log(f"[dim]iMessage error: {e}[/dim]")
    return results


def get_recent_whatsapp(minutes: int = 5, limit: int = 30) -> list[dict]:
    """Return list of {sender, text, dt, id_key} dicts from WhatsApp."""
    results = []
    try:
        db_path = os.path.expanduser(
            "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
        )
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cutoff_unix = (datetime.now() - timedelta(minutes=minutes)).timestamp()
        cutoff_apple = cutoff_unix - 978307200
        rows = conn.execute("""
            SELECT
                m.Z_PK,
                m.ZTEXT as text,
                datetime(m.ZMESSAGEDATE + 978307200, 'unixepoch', 'localtime') as dt,
                CASE WHEN m.ZISFROMME = 1 THEN 'me' ELSE
                    COALESCE(s.ZCONTACTJID, 'unknown')
                END as sender
            FROM ZWAMESSAGE m
            LEFT JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
            WHERE m.ZTEXT IS NOT NULL AND m.ZTEXT != '' AND m.ZMESSAGEDATE > ?
            ORDER BY m.ZMESSAGEDATE DESC
            LIMIT ?
        """, (cutoff_apple, limit)).fetchall()
        conn.close()
        for r in rows:
            sender = "You" if r["sender"] == "me" else r["sender"]
            text = (r["text"] or "")[:300]
            id_key = (sender, r["dt"], hashlib.md5(text.encode()).hexdigest()[:12])
            results.append({"sender": sender, "text": text, "dt": r["dt"],
                            "id_key": id_key, "source": "whatsapp"})
    except Exception as e:
        add_log(f"[dim]WhatsApp error: {e}[/dim]")
    return results


def get_chrome_tabs() -> list[dict]:
    """Return list of {title, url, id_key} dicts for open Chrome tabs."""
    results = []
    script = '''
    tell application "Google Chrome"
        set tabList to {}
        repeat with w in every window
            repeat with t in every tab of w
                set end of tabList to (title of t) & " ||| " & (URL of t)
            end repeat
        end repeat
        set AppleScript's text item delimiters to "\\n"
        return tabList as text
    end tell
    '''
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            for line in r.stdout.strip().split("\n"):
                parts = line.split(" ||| ", 1)
                title = parts[0].strip()
                url = parts[1].strip() if len(parts) > 1 else ""
                id_key = url or title
                results.append({"title": title, "url": url,
                                "id_key": id_key, "source": "chrome"})
    except Exception as e:
        add_log(f"[dim]Chrome error: {e}[/dim]")
    return results


def get_clipboard() -> dict | None:
    """Return {text, id_key} or None."""
    try:
        r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
        text = r.stdout.strip()[:500]
        if text:
            h = hashlib.md5(text.encode()).hexdigest()[:16]
            return {"text": text, "id_key": h, "source": "clipboard"}
    except Exception:
        pass
    return None


def get_active_app() -> str:
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first process whose frontmost is true'],
            capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else "Unknown"
    except Exception:
        return "Unknown"


def get_window_title() -> str:
    """Get the title of the frontmost window (works across all apps, no Automation needed)."""
    try:
        script = '''
tell application "System Events"
    set fp to first process whose frontmost is true
    if (count of windows of fp) > 0 then
        return name of front window of fp
    end if
end tell
return ""
'''
        r = subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def get_calendar_events(days_ahead: int = 7) -> str:
    """Fetch upcoming calendar events via the gws CLI tool."""
    try:
        now = datetime.now().astimezone()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        end_date = (now + timedelta(days=days_ahead)).replace(
            hour=23, minute=59, second=59, microsecond=0
        ).isoformat()
        params = json.dumps({
            "calendarId": "primary",
            "timeMin": today_start,
            "timeMax": end_date,
            "singleEvents": True,
            "orderBy": "startTime",
        })
        r = subprocess.run(
            ["gws", "calendar", "events", "list", "--params", params],
            capture_output=True, text=True, timeout=8,
        )
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
            items = data.get("items", [])
            if not items:
                return "(No calendar events)"
            lines = []
            for item in items:
                summary = item.get("summary", "(No title)")
                start = item.get("start", {})
                start_time = start.get("dateTime", start.get("date", ""))
                lines.append(f"  {start_time}: {summary}")
            return "\n".join(lines)
        return "(No calendar events)"
    except Exception as e:
        return f"(Calendar error: {e})"


def capture_screenshot() -> str | None:
    """Capture a screenshot to a temp file, return filepath or None."""
    import tempfile
    filepath = os.path.join(tempfile.gettempdir(), f"claude_snap_{os.getpid()}.png")
    try:
        r = subprocess.run(["screencapture", "-x", "-C", filepath],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and os.path.exists(filepath):
            return filepath
    except Exception:
        pass
    return None


def analyze_screen(filepath: str) -> dict | None:
    """Send a screenshot to Haiku vision. Returns {summary, app, page_title, key_details} or None."""
    import base64
    try:
        with open(filepath, "rb") as f:
            img_data = base64.standard_b64encode(f.read()).decode("utf-8")

        goals_text = ", ".join(g["name"] for g in goals_config.get("goals", []))

        resp = client.messages.create(
            model=ANALYSIS_MODEL,
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": img_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            f"User goals: {goals_text}\n\n"
                            "What app and content is on screen? Be specific about any data, names, schedules, URLs.\n"
                            "Reply ONLY with JSON on a single line, no newlines inside strings:\n"
                            '{"summary":"one sentence","app":"app name","page_title":"title","key_details":"specifics"}'
                        ),
                    },
                ],
            }],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown fences
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        # Try to extract JSON even if surrounded by text
        if "{" in raw and "}" in raw:
            raw = raw[raw.index("{"):raw.rindex("}") + 1]
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Fallback: use the raw text as summary
        if raw:
            return {"summary": raw[:150], "app": "", "page_title": "", "key_details": ""}
        return None
    except Exception as e:
        add_log(f"[dim]Screen error: {e}[/dim]")
        return None


# Flag to request an immediate screenshot (set by collect_signals on app/tab change)
screen_capture_requested = threading.Event()


# ── Logging / display helpers ────────────────────────────────────────────────

def add_log(line: str):
    """Add a timestamped line to the scrolling log."""
    ts = datetime.now().strftime("%H:%M:%S")
    with lock:
        log_lines.insert(0, f"[dim]{ts}[/dim] {line}")
        if len(log_lines) > 200:
            log_lines.pop()


# ── Markdown file writers ────────────────────────────────────────────────────

memory_lock = threading.Lock()

def append_memory(source: str, content: str):
    """Append a timestamped entry to desktop_memory.md."""
    ts = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    entry = f"\n---\n\n### {ts} — {source}\n\n{content}\n"
    with memory_lock:
        with open(DESKTOP_MEMORY_MD, "a") as f:
            f.write(entry)
    add_log(f"[bold bright_white]📝 Memory:[/bold bright_white] {source}: {content[:60]}")


def update_predicted_actions(matches: list):
    """Regenerate predicted_actions.md based on accumulated signals and goals.

    Uses Sonnet to synthesize the current state into actionable predictions.
    """
    with lock:
        all_signals = {}
        for gname, sigs in goal_signals.items():
            if sigs:
                all_signals[gname] = sigs[-10:]  # last 10 per goal

    if not all_signals:
        return

    # Read current goals
    goals_text = ""
    try:
        with open(GOALS_MD) as f:
            goals_text = f.read()
    except Exception:
        pass

    # Read current memory
    memory_text = ""
    try:
        with open(DESKTOP_MEMORY_MD) as f:
            memory_text = f.read()[-3000:]  # last 3K chars
    except Exception:
        pass

    signals_summary = ""
    for gname, sigs in all_signals.items():
        signals_summary += f"\n{gname} ({len(sigs)} signals):\n"
        for s in sigs:
            signals_summary += f"  - [{s.get('source', '?')}] {s.get('summary', '')}\n"

    prompt = f"""You maintain a "predicted next actions" file for a proactive assistant.

GOALS:
{goals_text}

RECENT DESKTOP MEMORY:
{memory_text[-2000:]}

ACCUMULATED SIGNALS:
{signals_summary}

Based on the goals and signals, write an updated predicted_actions.md file.
For each goal with signals, include:
- Priority level (HIGH/NORMAL/LOW)
- What happened (summarize the signals)
- What needs to happen next (concrete action steps)

For goals with no signals, note "No action needed" with brief status.

Write clean markdown. Start with "# Predicted Next Actions" and today's date.
Be specific — include names, dates, prices, phone numbers from the signals."""

    try:
        add_log(f"[bold bright_magenta]📊 PREDICTING:[/bold bright_magenta] Regenerating action plan...")
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        new_content = resp.content[0].text.strip()
        with memory_lock:
            with open(PREDICTED_ACTIONS_MD, "w") as f:
                f.write(new_content)
        add_log(f"[bold bright_magenta]📊 PREDICTED:[/bold bright_magenta] Actions updated in predicted_actions.md")
    except Exception as e:
        add_log(f"[dim]Predicted actions update error: {e}[/dim]")


def goal_color(goal_name: str) -> str:
    """Deterministic color for a goal."""
    names = [g["name"] for g in goals_config.get("goals", [])]
    idx = names.index(goal_name) if goal_name in names else 0
    return GOAL_COLORS[idx % len(GOAL_COLORS)]


# ── Signal collection (deduplicating) ───────────────────────────────────────

def read_inject_file() -> list[dict]:
    """Read and process signals from the .inject file. No locks, no logging — just data."""
    inject_file = os.path.join(BASE_DIR, ".inject")
    results = []
    try:
        if not os.path.exists(inject_file):
            return results
        with open(inject_file) as f:
            raw = f.read()
        os.remove(inject_file)
        for line in raw.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            body = json.loads(line)
            source = body.get("source", "injected")
            text = body.get("text", "")
            sender = body.get("sender", "unknown")
            goal_name = body.get("goal", "")
            if not text:
                continue
            results.append({
                "sender": sender, "text": text,
                "dt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "id_key": (sender, str(time.time()), hashlib.md5(text.encode()).hexdigest()[:12]),
                "source": source,
                "goal": goal_name,
            })
    except Exception:
        pass
    return results


def collect_signals() -> list[dict]:
    """Gather new (unseen) signals from all sources. Returns flat list."""
    global seen_clipboard_hash, current_tabs, current_app, current_window_title, last_tab_event, last_app_event, running_apps
    new_signals = []

    # iMessage
    for msg in get_recent_imessages():
        if msg["id_key"] not in seen_imessage_ids:
            seen_imessage_ids.add(msg["id_key"])
            new_signals.append(msg)
            add_log(f"[bold cyan]👁 iMessage[/bold cyan] {msg['sender']}: {msg['text'][:80]}")

    # WhatsApp
    for msg in get_recent_whatsapp():
        if msg["id_key"] not in seen_whatsapp_ids:
            seen_whatsapp_ids.add(msg["id_key"])
            new_signals.append(msg)
            add_log(f"[bold green]👁 WhatsApp[/bold green] {msg['sender']}: {msg['text'][:80]}")

    # Chrome tabs — update panel state, only log new ones
    tabs = get_chrome_tabs()
    current_tabs = [{"title": t["title"], "url": t.get("url", "")} for t in tabs]
    for tab in tabs:
        if tab["id_key"] not in seen_tab_urls:
            seen_tab_urls.add(tab["id_key"])
            new_signals.append(tab)
            last_tab_event = f"Opened: {tab['title'][:60]}"
            add_log(f"[bold yellow]👁 New tab[/bold yellow] {tab['title'][:80]}")
            screen_capture_requested.set()  # trigger immediate screenshot

    # Clipboard
    clip = get_clipboard()
    if clip and clip["id_key"] != seen_clipboard_hash:
        seen_clipboard_hash = clip["id_key"]
        new_signals.append(clip)
        add_log(f"[bold magenta]👁 Clipboard[/bold magenta] {clip['text'][:80]}")

    # Active app — update panel state, log switches, trigger screenshot
    active = get_active_app()
    if active and active not in IGNORED_APPS and active != current_app:
        if current_app:  # don't log the initial read
            last_app_event = f"Switched to {active}"
            add_log(f"[bold blue]👁 App[/bold blue] → {active}")
            if active in SCREENSHOT_APPS:
                screen_capture_requested.set()
        current_app = active

    # Window title — detect tab switches, window focus changes
    win_title = get_window_title()
    if win_title and win_title != current_window_title:
        if current_window_title:  # don't log the initial read
            last_tab_event = f"{win_title[:60]}"
            add_log(f"[bold yellow]👁 Window[/bold yellow] {win_title[:80]}")
            if current_app in SCREENSHOT_APPS:
                screen_capture_requested.set()
        current_window_title = win_title

    # Running apps (for panel display, no log)
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of every process whose background only is false'],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            running_apps = [a.strip() for a in r.stdout.strip().split(", ") if a.strip() not in IGNORED_APPS]
    except Exception:
        pass

    return new_signals


# ── LLM analysis ────────────────────────────────────────────────────────────

def build_analysis_prompt(pending_signals: list[dict]) -> str:
    """Build the prompt that asks Haiku to match signals to goals."""
    # Read goals from goals.md (the source of truth)
    goals_text = ""
    try:
        with open(GOALS_MD) as f:
            goals_text = f.read()
    except Exception:
        goals_text = ""

    # People are already included in goals.md under each goal's "Key People" section.
    # No need for a separate people list.

    # Summarise accumulated signals per goal
    accumulated = ""
    with lock:
        for gname, sigs in goal_signals.items():
            if sigs:
                accumulated += f"\n  {gname} ({len(sigs)} prior signals):"
                for s in sigs[-5:]:
                    accumulated += f"\n    - [{s.get('source', '?')}] {s.get('summary', s.get('text', '')[:80])}"

    signals_text = ""
    for s in pending_signals:
        src = s.get("source", "?")
        if src in ("imessage", "whatsapp"):
            signals_text += f"\n  [{src}] {s.get('sender', '?')}: {s.get('text', '')[:200]}"
        elif src == "chrome":
            signals_text += f"\n  [chrome] {s.get('title', s.get('text', ''))} — {s.get('url', '')[:120]}"
        elif src == "clipboard":
            signals_text += f"\n  [clipboard] {s.get('text', '')[:200]}"
        else:
            signals_text += f"\n  [{src}] {s.get('sender', '')}: {s.get('text', '')[:200]}"

    return textwrap.dedent(f"""\
        You are a goal-matching assistant. Given the user's goals, people, and new signals,
        determine which signals relate to which goals.

        GOALS (each goal includes its description and Key People — use these to determine which goal a signal belongs to):
        {goals_text}

        PREVIOUSLY ACCUMULATED SIGNALS:
        {accumulated if accumulated else "  (none yet)"}

        NEW SIGNALS:
        {signals_text if signals_text else "  (none)"}

        For each new signal that relates to a goal, output a JSON array of objects:
        [
          {{
            "signal_summary": "brief description of the signal",
            "source": "imessage|whatsapp|chrome|clipboard|screen|calendar|observation",
            "goal": "exact goal name from the list above",
            "confidence": "low|medium|high",
            "action": "suggested action if confidence is high, else null"
          }}
        ]

        Confidence guide:
        - "low": signal is tangentially related to a goal
        - "medium": signal is clearly relevant but more info needed (e.g., saw a schedule but haven't checked calendar yet)
        - "high": ONLY when multiple corroborating signals exist (e.g., schedule data + calendar availability + relevant messages) AND a concrete action is ready

        IMPORTANT RULES:
        - Use the EXACT goal name from the GOALS list. Do not rephrase it.
        - Pay close attention to which people belong to which goal. If a person is listed
          under a goal's "Key People", signals involving that person almost certainly relate
          to THAT goal, not others. For example, if "Coach Rob" is listed under "Miles Gymnastics",
          a message from Coach Rob is about gymnastics — not soccer, even if the word "carpool"
          or "schedule" appears.
        - Match based on the goal's full description and people list, not just keyword overlap.
        - If no signals match any goal, return an empty array: []
        Return ONLY the JSON array — no markdown, no explanation.
    """)


def run_analysis(pending_signals: list[dict]):
    """Send pending signals to Haiku, match to goals, write to markdown files."""
    if not pending_signals:
        return

    add_log(f"[bold bright_yellow]🧠 THINKING:[/bold bright_yellow] Analyzing {len(pending_signals)} signals against goals...")

    prompt = build_analysis_prompt(pending_signals)
    try:
        resp = client.messages.create(
            model=ANALYSIS_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        matches = json.loads(raw)
    except Exception as e:
        add_log(f"[bold red]Analysis error: {e}[/bold red]")
        return

    if not isinstance(matches, list):
        return

    # Log unmatched signals too — show the thinking
    matched_sources = set()
    goal_names = [g["name"] for g in goals_config.get("goals", [])]

    had_new_match = False

    for m in matches:
        gname_raw = m.get("goal", "")
        conf = m.get("confidence", "low")
        summary = m.get("signal_summary", "")
        source = m.get("source", "?")
        action = m.get("action")
        matched_sources.add(source)

        # Normalize goal name
        gname = gname_raw
        for name in goal_names:
            if name.lower() in gname_raw.lower() or gname_raw.lower() in name.lower():
                gname = name
                break

        color = goal_color(gname)

        # Accumulate in memory
        with lock:
            if gname not in goal_signals:
                goal_signals[gname] = []
            goal_signals[gname].append({
                "summary": summary,
                "source": source,
                "confidence": conf,
                "action": action,
                "time": datetime.now().strftime("%H:%M:%S"),
            })
            count = len(goal_signals[gname])

        # Show the match in the terminal
        add_log(f"[bold bright_green]  ✓ Match:[/bold bright_green] {summary}")
        add_log(f"[bold {color}]    → {gname}[/bold {color}] ({count} signal{'s' if count != 1 else ''}, {conf})")

        # Write to desktop_memory.md — ONLY for goal-matched signals
        append_memory(
            f"{source} → {gname} ({conf})",
            f"{summary}" + (f"\n\nSuggested action: {action}" if action else ""),
        )
        add_log(f"[bold bright_white]📝 RECORDED:[/bold bright_white] {source} → desktop_memory.md")
        had_new_match = True

    # Log signals that didn't match any goal
    for s in pending_signals:
        src = s.get("source", "?")
        if src not in matched_sources:
            text = s.get("text", s.get("title", ""))[:60]
            add_log(f"[dim]  ⊘ No goal match: [{src}] {text}[/dim]")

    # Update predicted_actions.md when we have new matches
    if had_new_match:
        # Run in background thread to avoid blocking the analysis loop
        threading.Thread(target=update_predicted_actions, args=(matches,), daemon=True).start()


# ── Display builder ──────────────────────────────────────────────────────────

def build_display() -> Group:
    """Build the rich renderable for the Live display."""
    # Goals panel
    goals_table = Table(show_header=False, box=None, padding=(0, 2))
    goals_table.add_column("icon", width=3)
    goals_table.add_column("goal", min_width=20)
    goals_table.add_column("signals", justify="right", width=12)
    goals_table.add_column("confidence", width=14)

    for g in goals_config.get("goals", []):
        name = g["name"]
        icon = g.get("icon", "\u2022")
        color = goal_color(name)
        with lock:
            sigs = goal_signals.get(name, [])
        count = len(sigs)
        if count == 0:
            conf_label = "[dim]waiting[/dim]"
        else:
            last_conf = sigs[-1].get("confidence", "low")
            conf_color = {"low": "yellow", "medium": "bright_yellow",
                          "high": "bright_red"}.get(last_conf, "white")
            conf_label = f"[{conf_color}]{last_conf}[/{conf_color}]"
        goals_table.add_row(
            icon,
            f"[bold {color}]{name}[/bold {color}] [dim]— {g['description'][:50]}[/dim]",
            f"{count} signal{'s' if count != 1 else ''}",
            conf_label,
        )

    goals_panel = Panel(
        goals_table,
        title="[bold]Active Goals[/bold]",
        border_style="bright_blue",
    )

    # Screen / Browser panel
    screen_lines = Text()
    if current_tabs:
        for t in current_tabs[:6]:
            title = t["title"][:55] if t["title"] else "(untitled)"
            url_short = t.get("url", "")
            if "://" in url_short:
                url_short = url_short.split("://", 1)[1].split("/")[0]
            screen_lines.append_text(Text.from_markup(
                f"  [dim]{url_short[:25]:25}[/dim]  {title}\n"
            ))
        if len(current_tabs) > 6:
            screen_lines.append_text(Text.from_markup(
                f"  [dim]... and {len(current_tabs) - 6} more tabs[/dim]\n"
            ))
        if last_tab_event:
            screen_lines.append_text(Text.from_markup(f"  [bold yellow]Latest:[/bold yellow] {last_tab_event}\n"))
    if last_screen_summary:
        screen_lines.append_text(Text.from_markup(
            f"  [bold bright_white]👁 Screen:[/bold bright_white] {last_screen_summary[:120]}\n"
        ))
    if not current_tabs and not last_screen_summary:
        screen_lines.append_text(Text.from_markup("[dim]  Waiting for first screen capture...[/dim]\n"))

    screen_title_parts = []
    if current_tabs:
        screen_title_parts.append(f"{len(current_tabs)} tabs")
    screen_title_parts.append(f"screen every {SCREEN_INTERVAL}s")

    browser_panel = Panel(
        screen_lines,
        title=f"[bold]Browser & Screen[/bold]  [dim]| {' · '.join(screen_title_parts)}[/dim]",
        border_style="yellow",
    )

    # Apps panel — running apps + active app
    apps_text = Text()
    if running_apps:
        app_strs = []
        for a in running_apps[:12]:
            if a == current_app:
                app_strs.append(f"[bold bright_white]{a}[/bold bright_white]")
            else:
                app_strs.append(f"[dim]{a}[/dim]")
        apps_text.append_text(Text.from_markup("  " + "  ·  ".join(app_strs) + "\n"))
    if last_app_event:
        apps_text.append_text(Text.from_markup(f"  [bold blue]Latest:[/bold blue] {last_app_event}\n"))

    apps_panel = Panel(
        apps_text or Text("[dim]  Scanning...[/dim]"),
        title=f"[bold]Apps[/bold]  [dim]| Active: {current_app or '?'}[/dim]",
        border_style="blue",
    )

    # Log panel — show last ~20 lines (shorter now with extra panels)
    with lock:
        visible = log_lines[:20]
    log_text = Text()
    for line in reversed(visible):
        log_text.append_text(Text.from_markup(line + "\n"))

    log_panel = Panel(
        log_text or Text("[dim]Waiting for signals...[/dim]"),
        title="[bold]Signal Log[/bold]",
        border_style="green",
        height=25,
    )

    return Group(goals_panel, browser_panel, apps_panel, log_panel)


# ── Main loops ───────────────────────────────────────────────────────────────

def signal_loop(pending: list, pending_lock: threading.Lock):
    """Collect signals every SIGNAL_INTERVAL seconds."""
    while True:
        try:
            new = collect_signals()
            if new:
                with pending_lock:
                    pending.extend(new)
        except Exception as e:
            add_log(f"[bold red]Signal loop error: {e}[/bold red]")
        time.sleep(SIGNAL_INTERVAL)


def analysis_loop(pending: list, pending_lock: threading.Lock):
    """Run LLM analysis every ANALYSIS_INTERVAL seconds."""
    time.sleep(5)  # brief startup wait
    while True:
        try:
            with pending_lock:
                batch = list(pending)
                pending.clear()
            if batch:
                run_analysis(batch)
        except Exception as e:
            add_log(f"[bold red]Analysis loop error: {e}[/bold red]")
        time.sleep(ANALYSIS_INTERVAL)


def calendar_loop():
    """Refresh calendar events every 5 minutes, feed as a signal."""
    last_hash = ""
    while True:
        try:
            cal = get_calendar_events(days_ahead=7)
            h = hashlib.md5(cal.encode()).hexdigest()[:16]
            if h != last_hash and "(error" not in cal.lower() and "(No calendar" not in cal:
                last_hash = h
                add_log(f"[bold blue]Calendar[/bold blue] refreshed")
        except Exception:
            pass
        time.sleep(300)


def screen_loop(pending: list, pending_lock: threading.Lock):
    """Capture and analyze screenshots — only triggered by app/tab/window changes."""
    global last_screen_summary
    last_hash = ""
    while True:
        # Block until something changes (app switch, tab switch, window title change)
        screen_capture_requested.wait()
        screen_capture_requested.clear()
        time.sleep(1.5)  # let the new content render

        try:
            path = capture_screenshot()
            if not path:
                continue

            # Downsample before hashing to ignore minor pixel changes
            # (cursor blink, clock updates, etc.) — read every 1KB chunk
            with open(path, "rb") as f:
                raw = f.read()
            sampled = raw[::1024]  # sample every 1KB
            h = hashlib.md5(sampled).hexdigest()[:12]
            if h == last_hash:
                os.remove(path)  # discard unchanged screenshot
                continue
            last_hash = h

            add_log("[dim]👁 Screen changed — analyzing...[/dim]")

            result = analyze_screen(path)

            # Delete screenshot immediately — we only keep the reasoning
            try:
                os.remove(path)
            except Exception:
                pass

            if result:
                summary = result.get("summary", "")
                app = result.get("app", "")
                page_title = result.get("page_title", "")
                details = result.get("key_details", "")
                last_screen_summary = summary

                screen_text = f"{page_title}. {details}" if page_title else summary
                if screen_text.strip() and screen_text.strip() != ".":
                    sig = {
                        "source": "screen",
                        "sender": app or "Screen",
                        "text": screen_text[:300],
                        "dt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "id_key": ("screen", datetime.now().isoformat()),
                    }
                    with pending_lock:
                        pending.append(sig)
                    add_log(f"[bold bright_white]👁 Screen[/bold bright_white] {summary[:80]}")

        except Exception as e:
            add_log(f"[dim]Screen loop error: {e}[/dim]")


# ── Entrypoint ───────────────────────────────────────────────────────────────

def main():
    global goals_config, client, goal_signals, ANALYSIS_INTERVAL

    # Load goals from goals.md
    if not os.path.exists(GOALS_MD):
        console.print(f"[bold red]Goals file not found: {GOALS_MD}[/bold red]")
        console.print("[dim]Create data/goals.md or run setup.py[/dim]")
        sys.exit(1)

    with open(GOALS_MD) as f:
        goals_md_text = f.read()

    # Parse goal names from ## headings
    import re
    goal_names_parsed = re.findall(r"^## (.+)$", goals_md_text, re.MULTILINE)
    goals_config = {"goals": [{"name": n, "description": "", "icon": "🎯"} for n in goal_names_parsed], "people": []}

    # Initialise per-goal signal buckets
    for name in goal_names_parsed:
        goal_signals[name] = []

    # Initialise Anthropic client
    api_key = get_api_key()
    client = Anthropic(api_key=api_key)

    # Startup banner
    console.print()
    console.print(Panel(
        "[bold bright_white]Desktop Context[/bold bright_white]\n"
        "[dim]Ambient signal monitoring for Claude.\n"
        "Press Ctrl+C to stop.[/dim]",
        border_style="bold bright_blue",
        padding=(1, 4),
    ))
    console.print()

    goals_list = goals_config.get("goals", [])
    console.print("[bold]Goals:[/bold]")
    for i, g in enumerate(goals_list, 1):
        icon = g.get("icon", "\u2022")
        color = goal_color(g["name"])
        console.print(f"  {i}. {icon} [{color}]{g['name']}[/{color}] — {g['description']}")
    console.print()

    sources = "iMessage, WhatsApp, Chrome, Clipboard, Calendar, Screen"
    console.print(f"[bold]Monitoring:[/bold] {sources}")
    console.print(f"[bold]Signal check:[/bold] every {SIGNAL_INTERVAL}s  "
                  f"[bold]Analysis:[/bold] every {ANALYSIS_INTERVAL}s  "
                  f"[bold]Model:[/bold] {ANALYSIS_MODEL}")
    console.print()

    # In demo mode, faster analysis + pre-scan existing signals
    demo_mode = "--fresh" in sys.argv
    if demo_mode:
        ANALYSIS_INTERVAL = 8  # faster for demos
        console.print("[bold yellow]DEMO MODE:[/bold yellow] Pre-scanning existing signals to ignore them...")
        # Do a silent first pass to mark all current messages/tabs/clipboard as seen
        collect_signals()  # This populates seen_imessage_ids, seen_whatsapp_ids, seen_tab_urls, seen_clipboard_hash
        console.print("[dim]Existing signals marked as seen. Only NEW signals will trigger.[/dim]")
        console.print()

    # Shared pending-signal buffer
    pending: list[dict] = []
    pending_lock = threading.Lock()

    # Start background threads
    threads = [
        threading.Thread(target=signal_loop, args=(pending, pending_lock), daemon=True),
        threading.Thread(target=analysis_loop, args=(pending, pending_lock), daemon=True),
        threading.Thread(target=calendar_loop, daemon=True),
        threading.Thread(target=screen_loop, args=(pending, pending_lock), daemon=True),
    ]
    for t in threads:
        t.start()

    # Live display
    try:
        with Live(build_display(), console=console, refresh_per_second=2,
                  screen=True) as live:
            while True:
                # Check inject file from main thread (no locks held)
                injected = read_inject_file()
                for sig in injected:
                    with pending_lock:
                        pending.append(sig)
                    add_log(f"[bold cyan]{sig['source']}[/bold cyan] {sig['sender']}: {sig['text'][:80]}")
                    goal_name = sig.get("goal", "")
                    if goal_name:
                        count = 0
                        color = goal_color(goal_name)
                        with lock:
                            if goal_name in goal_signals:
                                goal_signals[goal_name].append({
                                    "summary": sig["text"][:100],
                                    "source": f"{sig['sender']} via {sig['source']}",
                                    "confidence": "medium",
                                    "dt": sig["dt"],
                                })
                                count = len(goal_signals[goal_name])
                        if count:
                            add_log(f"           [bold {color}]-> {goal_name}[/bold {color}] ({count} signal{'s' if count != 1 else ''})")

                live.update(build_display())
                time.sleep(0.5)
    except KeyboardInterrupt:
        console.print("\n[bold bright_blue]Monitor stopped.[/bold bright_blue]")


if __name__ == "__main__":
    main()
