# Desktop Context Engine

**Ambient intelligence for Claude Desktop — it sees what you see, knows what you need.**

> macOS only. Requires Claude Desktop.

## How It Works

A terminal-based monitor watches your desktop activity, distills it into markdown files, and exposes them to Claude Desktop through MCP tools. Claude gains persistent, evolving context about what you're doing and what you care about — no copy-pasting, no manual summaries.

The system has two independent pieces that share markdown files:

1. **Monitor** (`monitor_terminal.py`) — A Rich terminal dashboard that observes your desktop, matches signals against your goals, and writes results to markdown.
2. **MCP Servers** (3 files in `mcps/`) — Expose those markdown files to Claude Desktop as tools.

```
┌─────────────────────────────────────────────┐
│              Claude Desktop                  │
│                                              │
│   "What should I be working on?"             │
│              ↓                               │
│   ┌───────────────────────────────┐         │
│   │  MCP Tools                     │         │
│   │  · get_desktop_memory()        │         │
│   │  · get_goals() / set_goal()    │         │
│   │  · get_predicted_actions()     │         │
│   └──────────────┬────────────────┘         │
│                  │                           │
│   Also available: iMessage, Gmail,           │
│   Calendar, Web Search (built-in)            │
└──────────────────┼───────────────────────────┘
                   │ reads
┌──────────────────┼───────────────────────────┐
│     data/        │                            │
│     ├── desktop_memory.md    ← Monitor writes │
│     ├── goals.md             ← You / Claude   │
│     └── predicted_actions.md ← Monitor writes │
└──────────────────┼───────────────────────────┘
                   │ writes
┌──────────────────┼───────────────────────────┐
│   Desktop Context Monitor (terminal)          │
│                                               │
│   OBSERVES                                    │
│   iMessage · WhatsApp · Chrome · Screen       │
│   Active Apps · Window Titles · Calendar       │
│                                               │
│   THINKS                                      │
│   Haiku matches signals to your goals          │
│   Only goal-relevant signals are recorded      │
│                                               │
│   RECORDS → desktop_memory.md                 │
│   PREDICTS → predicted_actions.md              │
└───────────────────────────────────────────────┘
```

## How the Monitor Works

The monitor runs four phases in a continuous loop, shown live in the terminal:

**OBSERVING** — Every 2 seconds, the monitor collects signals from all sources: iMessage (reads chat.db directly), WhatsApp (reads ChatStorage.sqlite), Chrome tabs and URLs (via AppleScript), active app name, and window titles. Screen capture happens only on app or window change and uses perceptual hashing (`imagehash`) to skip unchanged screens. Screenshots are never stored — captured to a temp file, analyzed by Haiku vision for text/context, then deleted immediately. Only the text summary survives.

**THINKING** — Every 8 seconds, Haiku receives the collected signals alongside your goals from `goals.md`. It determines which signals are relevant to what you care about. Noise (tab switches to unrelated sites, idle periods, irrelevant messages) is filtered out entirely.

**RECORDING** — Only goal-relevant signals get appended to `desktop_memory.md`. This keeps the file focused and useful rather than a firehose of raw activity.

**PREDICTING** — When new goal matches occur, Sonnet regenerates `predicted_actions.md` with an updated action plan based on what happened and what you care about.

## Requirements

**Claude Desktop connectors** (enable in Claude Desktop Settings > Connectors):
- **Claude in Chrome** — browser context awareness
- **Read and Send iMessages** — message reading and drafting
- **Gmail** — email search and reading
- **Google Calendar** — event awareness

**macOS permissions** (System Settings > Privacy & Security):

| Permission | Where | Why |
|---|---|---|
| Full Disk Access | Claude Desktop, Terminal | Read iMessage database (chat.db) |
| Accessibility | Terminal | Detect window titles, Chrome tabs |
| Automation > Chrome | Terminal | Read browser tab titles and URLs |
| Screen Recording | Terminal | Screenshot analysis for ambient context |
| Contacts | Claude Desktop | Resolve contact names |

The setup script will walk you through each permission.

**Software:**
- macOS 12+
- Python 3.10+
- Claude Desktop with an Anthropic API key (for the monitor's Haiku/Sonnet calls)

## Quick Start

```bash
git clone https://github.com/dwurtz/desktop_context_engine
cd desktop_context_engine
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Then either run the interactive setup or configure manually:

```bash
python3 setup.py          # interactive: goals, Claude Desktop config, permissions
```

Or manually: edit `data/goals.md`, then add the MCP servers to Claude Desktop config.

## Running the Monitor

```bash
source venv/bin/activate
export ANTHROPIC_API_KEY=your-key-here
python3 monitor_terminal.py --fresh
```

The `--fresh` flag pre-scans existing signals on startup so only new activity triggers analysis. The terminal displays a Rich dashboard showing all four phases (OBSERVING, THINKING, RECORDING, PREDICTING) with live signal counts and goal match status.

## Tip: Reliable Tool Usage in Claude Desktop

To ensure Claude consistently uses the desktop context tools, create a **Project** in Claude Desktop (e.g., "Life") and add this to the project instructions:

> Use the goals, predicted action and desktop context tools.

Then in the project's settings, set **Tool access mode** to **"Tools already loaded"**. This ensures the MCP tools are always available in the conversation context, so Claude will use them without needing to discover them first.

Then open Claude Desktop in that project and say: **"What should I be focused on right now?"**

## The Three Files

The entire system reduces to three markdown files that Claude reads via MCP:

- **desktop_memory.md** — Timestamped log of goal-relevant observations. Messages, browser tabs, screen content. Append-only, noise-filtered.
- **goals.md** — Your goals in natural language. What you care about, who's involved, how you want Claude to help. You write these (or Claude does via `set_goal`).
- **predicted_actions.md** — The monitor's analysis: given what happened and what you care about, here's what needs to happen next. Auto-generated by Sonnet.

## Project Structure

```
desktop_context_engine/
├── README.md
├── setup.py                      # Interactive setup wizard
├── requirements.txt
├── monitor_terminal.py           # Desktop Context Monitor (Rich terminal)
├── mcps/                         # MCP servers for Claude Desktop
│   ├── desktop_memory.py         # get_desktop_memory()
│   ├── goals.py                  # get_goals(), set_goal()
│   └── predict_next_action.py    # get_predicted_actions()
├── data/                         # Shared markdown files (gitignored)
│   ├── desktop_memory.md         # Raw signal log (monitor writes)
│   ├── goals.md                  # Your goals (you write)
│   └── predicted_actions.md      # Action predictions (monitor writes)
└── monitor/                      # (future: menu bar app)
    ├── app.py
    ├── signals.py
    ├── analysis.py
    └── writers.py
```

## Privacy

- **Screenshots are never stored.** Captured to a temp file, analyzed by Haiku vision, deleted immediately. Only the text summary persists in desktop_memory.md.
- **All data stays local** in markdown files on your machine. Nothing is sent to external services beyond the Anthropic API.
- **Allowlisted apps only.** The monitor captures screen content from Chrome, Messages, Calendar, Mail, and a handful of other apps. Terminal, code editors, and password managers are excluded.
