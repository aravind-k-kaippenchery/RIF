"""Small official-SDK client used to verify the local /mcp server after startup.

Run: python -m app.mcp.verify_client --url http://127.0.0.1:8000/mcp
"""

from __future__ import annotations

import argparse
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def verify(url: str) -> None:
    async with streamablehttp_client(url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = [tool.name for tool in tools.tools]
            print("MCP connection verified.")
            print("Registered tools:", ", ".join(tool_names))
            result = await session.call_tool("get_schema", {})
            print("get_schema tool call completed.")
            print(json.dumps(result.model_dump(mode="json"), indent=2, default=str)[:1500])


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the local B2B MCP server.")
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    args = parser.parse_args()
    asyncio.run(verify(args.url))


if __name__ == "__main__":
    main()
