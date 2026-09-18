# Runtime core

Keep runtime state, operation lifecycle, RDBG capture, BSL compilation, Worker generations, and value materialization here. Frontends must call public runtime contracts; core must not import Jupyter, MCP, or VS Code packages. After `RuntimeSession.start()`, one post-bootstrap controller and `RdbgArbiter` own execution. `runtime_models.py` owns public reply/status types; `capture_evaluation_models.py` and `capture_transfer_models.py` own CAPTURE data contracts.

Preserve single-writer operation semantics and capture fences. CAPTURE inspection requires the exact operation, route, frame, and target to remain valid. A detached waiter, failed cell, or elapsed poll interval does not prove target loss; do not report Stop success or mark the target lost without confirmed exit evidence. Changes to diagnostics, stack frames, or materialization must preserve private-data redaction and bounded results. For a behavior change, run focused `tests/unit` cases and relevant integration contract tests; live 1C qualification remains opt-in.

`bsl/generated_semantic_parser.py` is generated. Use `tools/generate_bsl_semantic_parser.py` and the configured parser-generator source; do not hand-edit the generated file.
