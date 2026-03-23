#!/usr/bin/env python3
"""Signal collectors — gather context from the Mac."""
import base64
import hashlib
import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta

# ── Module-level state for screen diffing ────────────────────────────────────

_last_screen_hash = None
_last_app = None
_last_window_title = None

SCREENSHOT_APPS = {
    "Google Chrome", "Safari", "Arc", "Brave Browser", "Firefox",
    "Messages", "WhatsApp", "Mail", "Superhuman", "Calendar",
    "Preview", "Finder", "Slack", "Notes", "Maps",
}


# ── Individual collectors ────────────────────────────────────────────────────

def get_recent_imessages(minutes: int = 5, limit: int = 20) -> list[dict]:
    """Read recent iMessages from chat.db."""
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
            results.append({
                "source": "imessage", "sender": sender,
                "text": text, "dt": r["dt"], "id_key": id_key,
            })
    except Exception as e:
        print(f"[signals] iMessage error: {e}")
    return results


def get_recent_whatsapp(minutes: int = 5, limit: int = 20) -> list[dict]:
    """Read recent WhatsApp messages from ChatStorage.sqlite."""
    results = []
    try:
        db_path = os.path.expanduser(
            "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
        )
        if not os.path.exists(db_path):
            return results
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
            results.append({
                "source": "whatsapp", "sender": sender,
                "text": text, "dt": r["dt"], "id_key": id_key,
            })
    except Exception as e:
        print(f"[signals] WhatsApp error: {e}")
    return results


def get_chrome_tabs() -> list[dict]:
    """Get open Chrome tab titles and URLs via osascript."""
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
                results.append({
                    "source": "chrome", "text": title, "sender": url,
                    "dt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "id_key": id_key,
                })
    except Exception as e:
        print(f"[signals] Chrome error: {e}")
    return results


def get_clipboard() -> dict | None:
    """Return clipboard contents as a signal dict, or None."""
    try:
        r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
        text = r.stdout.strip()[:500]
        if text:
            h = hashlib.md5(text.encode()).hexdigest()[:16]
            return {
                "source": "clipboard", "sender": "clipboard",
                "text": text,
                "dt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "id_key": h,
            }
    except Exception:
        pass
    return None


def get_active_app() -> str:
    """Get the name of the frontmost application."""
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first process whose frontmost is true'],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() if r.returncode == 0 else "Unknown"
    except Exception:
        return "Unknown"


def get_window_title() -> str:
    """Get the title of the frontmost window."""
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


def capture_and_analyze_screen(client) -> dict | None:
    """
    Capture screen, perceptual-hash to skip unchanged screens,
    send to Haiku vision for analysis, then delete the image.
    Returns a signal dict or None.
    """
    global _last_screen_hash, _last_app, _last_window_title
    import imagehash
    from PIL import Image

    # Only capture when app/window context has changed
    current_app = get_active_app()
    current_title = get_window_title()

    if current_app not in SCREENSHOT_APPS:
        return None

    if current_app == _last_app and current_title == _last_window_title:
        return None  # no context change

    _last_app = current_app
    _last_window_title = current_title

    # Capture
    path = tempfile.mktemp(suffix=".png")
    try:
        subprocess.run(["screencapture", "-x", "-C", path],
                       capture_output=True, timeout=5)
    except Exception:
        if os.path.exists(path):
            os.remove(path)
        return None

    if not os.path.exists(path):
        return None

    # Perceptual hash — skip if unchanged
    try:
        current_hash = imagehash.phash(Image.open(path))
        if _last_screen_hash is not None and current_hash - _last_screen_hash < 5:
            os.remove(path)
            return None
        _last_screen_hash = current_hash
    except Exception:
        pass  # proceed even if hashing fails

    # Analyze with vision
    try:
        with open(path, "rb") as f:
            img_data = base64.standard_b64encode(f.read()).decode("utf-8")

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
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
                            "Describe what you see on this screen in 1-2 sentences. "
                            "Focus on what app is shown, what content is visible, "
                            "and what the user appears to be doing."
                        ),
                    },
                ],
            }],
        )
        summary = resp.content[0].text.strip()
    except Exception as e:
        summary = f"(Screen analysis error: {e})"
    finally:
        # Always delete the screenshot
        if os.path.exists(path):
            os.remove(path)

    return {
        "source": "screen",
        "sender": current_app,
        "text": summary,
        "dt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "id_key": f"screen-{datetime.now().strftime('%H%M%S')}",
    }


def collect_all(seen_ids: set, client=None) -> list[dict]:
    """
    Run all collectors, deduplicate against seen_ids, return new signals.
    Updates seen_ids in place.
    """
    raw = []

    # Message collectors
    raw.extend(get_recent_imessages())
    raw.extend(get_recent_whatsapp())

    # Chrome tabs
    raw.extend(get_chrome_tabs())

    # Clipboard
    clip = get_clipboard()
    if clip:
        raw.append(clip)

    # Active app / window as signals
    app_name = get_active_app()
    win_title = get_window_title()
    if app_name and app_name != "Unknown":
        raw.append({
            "source": "active_app", "sender": "system",
            "text": f"Active app: {app_name}" + (f" — {win_title}" if win_title else ""),
            "dt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "id_key": f"app-{app_name}-{win_title}",
        })

    # Screen capture (only if client is available)
    if client:
        screen = capture_and_analyze_screen(client)
        if screen:
            raw.append(screen)

    # Deduplicate
    new_signals = []
    for sig in raw:
        key = sig.get("id_key")
        # Convert tuple keys to a hashable form
        if isinstance(key, tuple):
            key = str(key)
        if key and key not in seen_ids:
            seen_ids.add(key)
            new_signals.append(sig)

    return new_signals
