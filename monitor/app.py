#!/usr/bin/env python3
"""Desktop Context Monitor — macOS menu bar app."""
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime


def main():
    # ── Parse args ───────────────────────────────────────────────────
    verbose = "--verbose" in sys.argv

    # ── Paths ────────────────────────────────────────────────────────
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DATA_DIR = os.path.join(BASE_DIR, "data")
    os.makedirs(DATA_DIR, exist_ok=True)

    GOALS_MD = os.path.join(DATA_DIR, "goals.md")
    DESKTOP_MEMORY_MD = os.path.join(DATA_DIR, "desktop_memory.md")
    PREDICTED_ACTIONS_MD = os.path.join(DATA_DIR, "predicted_actions.md")
    TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    LOG_FILE = os.path.join(DATA_DIR, "monitor.log")
    FLASK_PORT = 5055

    # ── Redirect output to log file (unless --verbose) ───────────────
    if not verbose:
        log_fh = open(LOG_FILE, "a")
        sys.stdout = log_fh
        sys.stderr = log_fh

    # ── Imports ───────────────────────────────────────────────────────
    import rumps
    from flask import Flask, jsonify, render_template
    from anthropic import Anthropic
    from monitor.signals import collect_all
    from monitor.analysis import match_signals_to_goals, update_predictions
    from monitor.writers import append_memory, read_file

    # ── Load .env if present ────────────────────────────────────────
    env_file = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())

    # ── API key ──────────────────────────────────────────────────────
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        if verbose:
            print("ERROR: ANTHROPIC_API_KEY required")
        else:
            import rumps as r
            r.alert("Desktop Context", "Set ANTHROPIC_API_KEY environment variable.")
        sys.exit(1)

    client = Anthropic(api_key=api_key)

    # ── Load goals ───────────────────────────────────────────────────
    goals_text = read_file(GOALS_MD) if os.path.exists(GOALS_MD) else ""
    print(f"[app] Goals loaded ({len(goals_text)} chars)", flush=True)

    # ── Shared state ─────────────────────────────────────────────────
    lock = threading.Lock()
    seen_ids = set()
    goal_signals = {}
    pending_signals = []
    all_signals = []
    state = {"signal_count": 0, "match_count": 0, "paused": False}

    # ── Flask log server ─────────────────────────────────────────────
    flask_app = Flask(__name__, template_folder=TEMPLATE_DIR)

    @flask_app.route("/")
    def index():
        return render_template("log.html")

    @flask_app.route("/api/log")
    def api_log():
        with lock:
            entries = list(reversed(all_signals[-200:]))
            match_entries = []
            for goal, sigs in goal_signals.items():
                for s in sigs[-5:]:
                    match_entries.append({
                        "source": "match",
                        "sender": goal,
                        "text": f"{s['summary']} ({s['confidence']})",
                        "dt": s.get("dt", ""),
                    })
            return jsonify({
                "signal_count": state["signal_count"],
                "match_count": state["match_count"],
                "status": "Paused" if state["paused"] else "Monitoring",
                "entries": (match_entries + entries)[:200],
            })

    def run_flask():
        log = logging.getLogger("werkzeug")
        log.setLevel(logging.ERROR)
        flask_app.run(host="127.0.0.1", port=FLASK_PORT, threaded=True, use_reloader=False)

    # ── Signal collection thread ─────────────────────────────────────
    def signal_loop():
        while True:
            if not state["paused"]:
                try:
                    new = collect_all(seen_ids, client)
                    if new:
                        with lock:
                            pending_signals.extend(new)
                            all_signals.extend(new)
                            if len(all_signals) > 500:
                                del all_signals[:len(all_signals) - 500]
                            state["signal_count"] += len(new)
                        for sig in new:
                            if sig["source"] in ("imessage", "whatsapp", "screen", "email"):
                                append_memory(
                                    DESKTOP_MEMORY_MD,
                                    sig["source"],
                                    f"**{sig.get('sender', '')}**: {sig['text'][:200]}"
                                )
                except Exception as e:
                    print(f"[signal] error: {e}", flush=True)
            time.sleep(2)

    # ── Analysis thread ──────────────────────────────────────────────
    def analysis_loop():
        time.sleep(5)
        while True:
            if not state["paused"]:
                try:
                    with lock:
                        batch = list(pending_signals)
                        pending_signals.clear()
                    if batch and goals_text.strip():
                        matches = match_signals_to_goals(
                            client, batch, goal_signals, goals_text
                        )
                        with lock:
                            state["match_count"] += len(matches)
                        if matches:
                            update_predictions(
                                client, goal_signals,
                                GOALS_MD, DESKTOP_MEMORY_MD, PREDICTED_ACTIONS_MD
                            )
                except Exception as e:
                    print(f"[analysis] error: {e}", flush=True)
            time.sleep(8)

    # ── Start background threads ─────────────────────────────────────
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=signal_loop, daemon=True).start()
    threading.Thread(target=analysis_loop, daemon=True).start()
    print(f"[app] Started — Flask on :{FLASK_PORT}", flush=True)
    print("[app] Starting menu bar...", flush=True)

    # ── Menu bar app (runs on main thread) ───────────────────────────
    class Monitor(rumps.App):
        def __init__(self):
            super().__init__(
                name="Desktop Context",
                title="DC",
                menu=[
                    rumps.MenuItem("Status: Starting...", callback=None),
                    None,
                    rumps.MenuItem("Show Log..."),
                    None,
                    rumps.MenuItem("Open Goals..."),
                    rumps.MenuItem("Open Desktop Memory..."),
                    rumps.MenuItem("Open Predicted Actions..."),
                    None,
                    rumps.MenuItem("Pause"),
                ],
            )
            self._status = self.menu["Status: Starting..."]

        @rumps.timer(2)
        def tick(self, _):
            sc = state["signal_count"]
            mc = state["match_count"]
            p = "||" if state["paused"] else "DC"
            self.title = f"{p} {sc}"
            s = "Paused" if state["paused"] else "Monitoring"
            self._status.title = f"{s} — {sc} signals, {mc} matches"

        @rumps.clicked("Show Log...")
        def show_log(self, _):
            subprocess.run(["open", f"http://127.0.0.1:{FLASK_PORT}"])

        @rumps.clicked("Open Goals...")
        def open_goals(self, _):
            subprocess.run(["open", GOALS_MD])

        @rumps.clicked("Open Desktop Memory...")
        def open_memory(self, _):
            subprocess.run(["open", DESKTOP_MEMORY_MD])

        @rumps.clicked("Open Predicted Actions...")
        def open_predictions(self, _):
            subprocess.run(["open", PREDICTED_ACTIONS_MD])

        @rumps.clicked("Pause")
        def toggle_pause(self, sender):
            state["paused"] = not state["paused"]
            sender.title = "Resume" if state["paused"] else "Pause"

    try:
        Monitor().run()
    except Exception as e:
        print(f"[app] FATAL: {e}", flush=True)
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
