from __future__ import annotations

import argparse
from pathlib import Path
import sys


WORKSPACE = Path(__file__).resolve().parents[1]
SRC = WORKSPACE / "src"
for entry in (WORKSPACE, SRC, WORKSPACE / "packages" / "jupyter" / "src"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from integration.jupyter_bsl_fixture.notebook import (  # noqa: E402
    build_fixture_notebook,
    serialize_fixture_notebook,
    verify_fixture_notebook,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = WORKSPACE / "tests" / "fixtures" / "notebooks" / "jupyter-bsl-fixture-acceptance.ipynb"
    expected = serialize_fixture_notebook(build_fixture_notebook())
    if args.check:
        if not path.is_file() or path.read_text(encoding="utf-8") != expected:
            raise SystemExit("Jupyter BSL fixture notebook is out of date")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8", newline="\n")
    verify_fixture_notebook(path, allow_outputs=False)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
