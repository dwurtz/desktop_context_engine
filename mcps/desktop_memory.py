#!/usr/bin/env python3
"""MCP server that exposes recent desktop activity captured by the ambient monitor."""
import os
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

server = Server("desktop-memory")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="get_desktop_memory",
            description=(
                "Returns a timestamped log of recent activity observed on the user's "
                "computer — emails, messages, browser tabs, clipboard, calendar events, "
                "and screen content. An ambient monitor captures this continuously, so "
                "it often already contains the answer to the user's question without "
                "needing to search calendar, email, or other sources."
            ),
            inputSchema={"type": "object", "properties": {}},
        )
    ]


@server.call_tool()
async def call_tool(name, arguments):
    if name == "get_desktop_memory":
        path = os.path.join(BASE_DIR, "data", "desktop_memory.md")
        try:
            with open(path, "r") as f:
                result = f.read()
        except FileNotFoundError:
            result = "(No desktop memory recorded yet. The monitor may not be running.)"
        return [TextContent(type="text", text=result)]
    raise ValueError(f"Unknown tool: {name}")


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
