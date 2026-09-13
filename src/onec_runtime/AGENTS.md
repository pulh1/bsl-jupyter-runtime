# Runtime core

Keep runtime state, operation lifecycle, RDBG capture, BSL compilation, and value materialization here. Frontends must call public runtime contracts; core must not import Jupyter, MCP, or VS Code packages.

Preserve single-writer operation semantics and capture fences. A stopped call may be inspected only while its operation and target remain active. Changes to diagnostics, stack frames, or materialization must preserve private-data redaction and bounded results. For a behavior change, run the focused `tests/unit` cases around the touched module and the relevant integration contract tests.

`bsl/generated_semantic_parser.py` is generated. Use `tools/generate_bsl_semantic_parser.py` and the configured parser-generator source; do not hand-edit the generated file.
