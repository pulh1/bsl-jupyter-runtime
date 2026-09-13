# Tests and fixtures

Keep synthetic 1C sources under `fixtures/onec`, acceptance notebooks under `fixtures/notebooks`, and the pinned parser-generator source under `fixtures/parsergen`. The small `fixtures/waterfall` set contains only files used by its regression verifier; do not add full live-run directories or process transcripts. A test fixture must be self-contained and free of personal data. If a notebook is generated, change its builder first, regenerate the checked-in file, and run its `--check` mode.

Unit tests should run without a 1C installation unless explicitly marked otherwise. Live tests in `integration/` must be opt-in, use a disposable infobase, and record evidence outside the repository. Prefer a focused behavior test over assertions that merely mirror implementation text.
