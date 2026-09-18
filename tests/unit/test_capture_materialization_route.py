"""Regression for a confirmed target-side CAPTURE materialization limit."""

import json

import pytest

from onec_runtime.errors import MaterializationLimitError
from onec_runtime.value_materialization import MaterializationOptions, decode_value_payload


def test_target_limit_envelope_routes_to_typed_value_error() -> None:
    payload = json.dumps(
        {"version": 1, "error": {"kind": "item_limit", "path": "$", "limit": 1}},
        separators=(",", ":"),
    ).encode("utf-8")

    with pytest.raises(MaterializationLimitError, match="items limit 1"):
        decode_value_payload(payload, MaterializationOptions(max_items=1))
