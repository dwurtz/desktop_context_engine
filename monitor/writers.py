#!/usr/bin/env python3
"""Markdown file writers for desktop memory."""
import os
from datetime import datetime


def append_memory(path: str, source: str, content: str):
    """Append a timestamped entry to desktop_memory.md."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"\n### [{ts}] {source}\n{content}\n"

    # Create file with header if it doesn't exist
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write("# Desktop Memory\n\nAutomatically collected context from your desktop.\n")

    with open(path, "a") as f:
        f.write(entry)


def read_file(path: str) -> str:
    """Read a markdown file, returns contents or empty string."""
    if os.path.exists(path):
        with open(path, "r") as f:
            return f.read()
    return ""
