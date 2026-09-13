"""Record a deterministic MCP tool-palette baseline without a runtime."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
from typing import Never

from mcp.client import Client

from onec_runtime_mcp.agent.mcp_profiles import McpProfile
from onec_runtime_mcp.server import create_mcp_server


class _NoRuntimeClient:
    """The measurement only asks the MCP SDK for schemas; calls are a defect."""

    def call(self, method: str, arguments: dict[str, object]) -> Never:
        raise AssertionError(f"palette measurement must not call {method}")


_JSON_LEXEME = re.compile(
    r'"(?:\\.|[^"\\])*"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|true|false|null|[{}\[\],:]'
)


async def _tool_schema(profile: McpProfile) -> list[dict[str, object]]:
    async with Client(create_mcp_server(_NoRuntimeClient(), profile=profile)) as client:
        tools = (await client.list_tools()).tools
    return sorted(
        (tool.model_dump(mode="json") for tool in tools),
        key=lambda tool: str(tool["name"]),
    )


def measure(profile: McpProfile) -> dict[str, object]:
    """Return the canonical static tool-list representation for *profile*."""
    tools = asyncio.run(_tool_schema(profile))
    canonical = json.dumps(
        {"tools": tools},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    encoded = canonical.encode("utf-8")
    return {
        "profile": profile.value,
        "tool_count": len(tools),
        "tool_names": [tool["name"] for tool in tools],
        "schema_encoding": "canonical-json-utf8",
        "schema_tokenizer": "json-lexeme-v1",
        "schema_bytes": len(encoded),
        "schema_tokens": len(_JSON_LEXEME.findall(canonical)),
        "schema_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record the deterministic MCP schema baseline without starting a runtime."
    )
    parser.add_argument("--profile", required=True, choices=tuple(McpProfile))
    parser.add_argument("--output", required=True)
    parsed = parser.parse_args(argv)
    destination = Path(parsed.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(measure(McpProfile(parsed.profile)), ensure_ascii=False, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
