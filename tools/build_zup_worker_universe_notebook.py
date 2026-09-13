from __future__ import annotations

import argparse
from pathlib import Path
import sys


WORKSPACE = Path(__file__).resolve().parents[1]
SRC = WORKSPACE / "src"
for entry in (WORKSPACE, SRC, WORKSPACE / "packages" / "jupyter" / "src"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from integration.zup_worker_universe_notebook import (  # noqa: E402
    build_notebook,
    serialize_notebook,
    verify_notebook,
)


def _validated_notebook_source() -> str:
    expected = serialize_notebook(build_notebook())
    if (
        "onec-worker-universe-zup-acceptance-v2" not in expected
        or "catalog_setup_ms" not in expected
        or "catalog_extension" not in expected
        or "active_generation_unchanged" not in expected
        or "incompatible_instrumentation" in expected
        or "atomic_promotion_phase_split_unavailable" in expected
    ):
        raise SystemExit("ZUP Worker universe acceptance notebook contract is stale")
    return expected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = WORKSPACE / "tests" / "fixtures" / "notebooks" / "zup-worker-universe-acceptance.ipynb"
    expected = _validated_notebook_source()
    if args.check:
        if not path.is_file() or path.read_text(encoding="utf-8") != expected:
            raise SystemExit("ZUP Worker universe acceptance notebook is out of date")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8", newline="\n")
    verify_notebook(path, allow_outputs=False)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
