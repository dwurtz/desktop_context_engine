#!/usr/bin/env python3
"""Claude Desktop Context — Setup Wizard"""

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
VENV_DIR = PROJECT_DIR / "venv"
REQUIREMENTS = PROJECT_DIR / "requirements.txt"

CLAUDE_CONFIG_PATH = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def try_rich():
    """Try to import rich, fall back to plain print."""
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.prompt import Prompt, Confirm
        return Console(), Panel, Prompt, Confirm
    except ImportError:
        return None, None, None, None


console, Panel, Prompt, Confirm = try_rich()


def print_header(text):
    if console and Panel:
        console.print(Panel(text, style="bold cyan"))
    else:
        print(f"\n{'=' * 60}")
        print(f"  {text}")
        print(f"{'=' * 60}")


def print_step(text):
    if console:
        console.print(f"  [green]✓[/green] {text}")
    else:
        print(f"  ✓ {text}")


def print_info(text):
    if console:
        console.print(f"  [dim]{text}[/dim]")
    else:
        print(f"  {text}")


def print_error(text):
    if console:
        console.print(f"  [red]✗[/red] {text}")
    else:
        print(f"  ✗ {text}")


def ask(prompt_text, default=None):
    if Prompt:
        return Prompt.ask(f"  {prompt_text}", default=default)
    else:
        suffix = f" [{default}]" if default else ""
        result = input(f"  {prompt_text}{suffix}: ").strip()
        return result if result else default


def ask_confirm(prompt_text, default=True):
    if Confirm:
        return Confirm.ask(f"  {prompt_text}", default=default)
    else:
        suffix = " [Y/n]" if default else " [y/N]"
        result = input(f"  {prompt_text}{suffix}: ").strip().lower()
        if not result:
            return default
        return result in ("y", "yes")


# ── Step 1: Check Python version ──

def check_python():
    print_header("Step 1 — Checking Python Version")
    v = sys.version_info
    if v >= (3, 10):
        print_step(f"Python {v.major}.{v.minor}.{v.micro} detected")
    else:
        print_error(f"Python 3.10+ required, found {v.major}.{v.minor}.{v.micro}")
        sys.exit(1)


# ── Step 2: Create venv ──

def setup_venv():
    print_header("Step 2 — Virtual Environment")
    if VENV_DIR.exists():
        print_step(f"venv already exists at {VENV_DIR}")
    else:
        print_info("Creating virtual environment...")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
        print_step(f"Created venv at {VENV_DIR}")

    # Return path to the venv python
    venv_python = VENV_DIR / "bin" / "python"
    if not venv_python.exists():
        print_error("Could not find venv python binary")
        sys.exit(1)
    return venv_python


# ── Step 3: Install requirements ──

def install_requirements(venv_python):
    print_header("Step 3 — Installing Dependencies")
    if not REQUIREMENTS.exists():
        print_error("requirements.txt not found")
        sys.exit(1)

    print_info("Installing packages (this may take a minute)...")
    subprocess.check_call(
        [str(venv_python), "-m", "pip", "install", "-r", str(REQUIREMENTS), "-q"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print_step("All dependencies installed")


# ── Step 4: Interactive goal setup ──

def setup_goals():
    print_header("Step 4 — Goal Setup")
    DATA_DIR.mkdir(exist_ok=True)

    goals_file = DATA_DIR / "goals.md"
    if goals_file.exists():
        if not ask_confirm("data/goals.md already exists. Overwrite with new goals?", default=False):
            print_info("Keeping existing goals.md")
            return

    goals = []
    while True:
        print()
        name = ask("What's a goal you'd like Claude to help with?")
        if not name:
            break
        description = ask("Describe it in a sentence or two")
        icon = ask("Pick an emoji icon", default="🎯")

        people = []
        print_info("Key people involved? (name, relationship, contact — one per line, blank to finish)")
        while True:
            person = ask("  Person")
            if not person:
                break
            people.append(person)

        goals.append({"name": name, "description": description, "icon": icon, "people": people})

        if not ask_confirm("Add another goal?", default=False):
            break

    if not goals:
        print_info("No goals entered. You can edit data/goals.md later.")
        return

    # Write goals.md
    lines = ["# Goals", "", "---"]
    for g in goals:
        lines.append("")
        lines.append(f"## {g['name']}")
        lines.append(f"{g['icon']} | Active")
        lines.append("")
        lines.append(g["description"])
        if g["people"]:
            lines.append("")
            lines.append("**Key People:**")
            for p in g["people"]:
                lines.append(f"- {p}")
        lines.append("")
        lines.append("---")

    goals_file.write_text("\n".join(lines) + "\n")
    print_step(f"Wrote {len(goals)} goal(s) to data/goals.md")


# ── Step 5: Configure Claude Desktop ──

def configure_claude_desktop(venv_python):
    print_header("Step 5 — Claude Desktop Configuration")

    if not CLAUDE_CONFIG_PATH.parent.exists():
        print_info("Claude Desktop config directory not found — skipping")
        return

    config = {}
    if CLAUDE_CONFIG_PATH.exists():
        with open(CLAUDE_CONFIG_PATH) as f:
            config = json.load(f)
        print_step("Read existing claude_desktop_config.json")
    else:
        print_info("No existing config found — creating new one")

    python_path = str(venv_python)

    mcp_servers = config.get("mcpServers", {})

    mcp_servers["desktop-memory"] = {
        "command": python_path,
        "args": [str(PROJECT_DIR / "mcps" / "desktop_memory.py")],
    }
    mcp_servers["goals"] = {
        "command": python_path,
        "args": [str(PROJECT_DIR / "mcps" / "goals.py")],
    }
    mcp_servers["predict-next-action"] = {
        "command": python_path,
        "args": [str(PROJECT_DIR / "mcps" / "predict_next_action.py")],
    }

    config["mcpServers"] = mcp_servers

    with open(CLAUDE_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

    print_step("Added 3 MCP servers to Claude Desktop config:")
    print_info("  • desktop-memory")
    print_info("  • goals")
    print_info("  • predict-next-action")
    print()
    if console:
        console.print("  [bold yellow]→ Restart Claude Desktop to load the new tools[/bold yellow]")
    else:
        print("  → Restart Claude Desktop to load the new tools")


# ── Step 6: Start monitor? ──

def offer_monitor():
    print_header("Step 6 — Desktop Monitor")
    print_info("The monitor captures ambient context from your screen.")
    print()
    if ask_confirm("Would you like to start the monitor now?", default=False):
        print()
        print_info("Run the following command:")
        print()
        if console:
            console.print(f"  [bold]cd {PROJECT_DIR} && source venv/bin/activate && python monitor_terminal.py[/bold]")
        else:
            print(f"  cd {PROJECT_DIR} && source venv/bin/activate && python monitor_terminal.py")
        print()
    else:
        print_info("You can start it later with: python monitor_terminal.py")


# ── Main ──

def main():
    print()
    print_header("Claude Desktop Context — Setup Wizard")
    print()

    check_python()
    venv_python = setup_venv()
    install_requirements(venv_python)
    setup_goals()
    configure_claude_desktop(venv_python)
    offer_monitor()

    print()
    print_header("Setup Complete!")
    print()


if __name__ == "__main__":
    main()
