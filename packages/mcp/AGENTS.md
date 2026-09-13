# MCP adapter

MCP tools and agent-service transport live here; runtime execution remains in `onec_runtime`. Keep wire contracts stable, validate requests at the boundary, and return bounded, privacy-safe observations. Do not expose private RDBG details, raw credentials, or unbounded values in tool responses or logs.

Run focused `tests/unit/test_mcp_*` and `tests/unit/test_agent_*` cases for touched contracts. Live 1C/MCP tests under `tests/integration` are opt-in and must use temporary evidence locations.
