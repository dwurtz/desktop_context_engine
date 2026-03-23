#!/usr/bin/env python3
"""MCP server that returns predicted next actions based on desktop memory and goals."""
import os
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

server = Server("predict-next-action")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="get_predicted_actions",
            description=(
                "Returns an AI-generated action plan based on the user's goals and "
                "recent desktop activity. Includes prioritized next steps, key contacts, "
                "and context. Often contains what the user needs without further lookups."
            ),
            inputSchema={"type": "object", "properties": {}},
        )
    ]


@server.call_tool()
async def call_tool(name, arguments):
    if name == "get_predicted_actions":
        path = os.path.join(BASE_DIR, "data", "predicted_actions.md")
        try:
            with open(path, "r") as f:
                result = f.read()
        except FileNotFoundError:
            result = "(No action predictions available. The monitor analyzes signals and generates predictions automatically.)"
        return [TextContent(type="text", text=result)]
    raise ValueError(f"Unknown tool: {name}")


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
