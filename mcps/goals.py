#!/usr/bin/env python3
"""MCP server for managing user goals with key people and contact details."""
import os
import re
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

server = Server("goals")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _goals_path():
    return os.path.join(BASE_DIR, "data", "goals.md")


def _read_goals():
    try:
        with open(_goals_path(), "r") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _write_goals(content):
    os.makedirs(os.path.dirname(_goals_path()), exist_ok=True)
    with open(_goals_path(), "w") as f:
        f.write(content)


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="get_goals",
            description=(
                "Returns the user's active goals with key people and contact details. "
                "Check this alongside desktop_memory and predicted_actions to understand "
                "what the user cares about before using other tools."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="set_goal",
            description=(
                "Create or update a goal. Goals should be high-level and stable — "
                "describe what to stay on top of, not specific tasks."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Goal name"},
                    "description": {"type": "string", "description": "Goal description"},
                    "icon": {"type": "string", "description": "Emoji icon", "default": "\U0001f3af"},
                    "people": {
                        "type": "string",
                        "description": "Key people formatted as a markdown list",
                    },
                },
                "required": ["name", "description"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name, arguments):
    if name == "get_goals":
        content = _read_goals()
        if content is None:
            content = "(No goals configured yet. Run setup.py to get started.)"
        return [TextContent(type="text", text=content)]

    if name == "set_goal":
        goal_name = arguments["name"]
        description = arguments["description"]
        icon = arguments.get("icon", "\U0001f3af")
        people = arguments.get("people", "- (none specified)")

        section = f"\n---\n\n## {goal_name}\n{icon} | Active\n\n{description}\n\n**Key People:**\n{people}\n"

        content = _read_goals() or ""

        # Look for existing section with matching name (case-insensitive)
        pattern = re.compile(
            r"(\n---\n\n## " + re.escape(goal_name) + r"\n.*?)(?=\n---\n\n## |\Z)",
            re.IGNORECASE | re.DOTALL,
        )
        match = pattern.search(content)

        if match:
            content = content[: match.start()] + section + content[match.end() :]
            action = "Updated"
        else:
            content = content.rstrip() + section
            action = "Created"

        _write_goals(content)
        return [TextContent(type="text", text=f"{action} goal '{goal_name}'")]

    raise ValueError(f"Unknown tool: {name}")


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
