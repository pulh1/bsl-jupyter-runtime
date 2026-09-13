# Waterfall regression fixtures

`run-a` and `run-b` contain only the four files read by
`verify_main_cell_waterfall_artifact`: frontend events, waterfall events,
waterfall summary, and final summary. They were selected from two historical
Jupyter adapter acceptance runs in the source repository (14 August 2026).
The records contain synthetic marker values and timing/operation metadata;
connection details, process transcripts, and full runtime artifacts are not
included. Keep these fixtures under `tests/fixtures`, not `artifacts/`.
