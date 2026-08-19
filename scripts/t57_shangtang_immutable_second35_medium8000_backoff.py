#!/usr/bin/env python3
from __future__ import annotations

import t53_shangtang_immutable_first48_low5000_backoff as base

base.MAX_TOKENS = 8000
base.REASONING_EFFORT = "medium"

_original_write_json = base.write_json

def _write_json(path, value):
    if getattr(path, "name", "") == "full_context_raw_result.json" and isinstance(value, dict):
        value = dict(value)
        value["schema_version"] = "research_immutable_second35_medium8000_raw_result.v1"
    _original_write_json(path, value)

base.write_json = _write_json

if __name__ == "__main__":
    raise SystemExit(base.main())
