# MCP adapter

MCP tools, agent-service transport, operation views, and proxy publication live here; MAIN/CAPTURE execution, Worker publication, and RDBG ownership remain in `onec_runtime`. Keep wire contracts stable, validate requests at the boundary, and return bounded, privacy-safe observations. Do not expose private RDBG details, raw credentials, or unbounded values in tool responses or logs.

Seed existing `bsl.<name>` proxies from the runtime namespace snapshot on admission; publish changed bindings after confirmed MAIN completion with matching provenance. Their resolver handles use `e1cRuntimeКонтекст.<name>` and remain generation-fenced; do not publish raw values as bindings. Keep stopped CAPTURE inspection and continuation tied to the exact capture fence, and preserve the runtime's uncertain outcome instead of treating a timeout as completion.

Run focused `tests/unit/test_mcp_*` and `tests/unit/test_agent_*` cases for touched contracts. Live 1C/MCP tests under `tests/integration` are opt-in and must use temporary evidence locations.
