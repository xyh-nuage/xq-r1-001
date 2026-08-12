#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics
from typing import Any

from l1_processor.llm_client import OpenAICompatibleClient
from l1_processor.local_semantics import extract_local_semantics

ROW_PREFIX = "CONTINUOUS_SLICE_A_ROW="
PROBE_SPECS = (
    ("A01", "2026-07-06", 4),
    ("A02", "2026-07-06", 13),
    ("A03", "2026-07-06", 19),
    ("A04", "2026-07-06", 20),
    ("A05", "2026-07-06", 21),
    ("A06", "2026-07-06", 26),
    ("A07", "2026-07-06", 30),
    ("A08", "2026-07-06", 40),
    ("A09", "2026-07-06", 33),
    ("A10", "2026-07-06", 89),
    ("A11", "2026-07-07", 3),
    ("A12", "2026-07-07", 4),
    ("A13", "2026-07-07", 6),
    ("A14", "2026-07-07", 15),
    ("A15", "2026-07-07", 22),
    ("A16", "2026-07-07", 26),
    ("A17", "2026-07-07", 34),
    ("A18", "2026-07-07", 42),
)


def _read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(ROW_PREFIX):
            value = json.loads(line[len(ROW_PREFIX):])
            if not isinstance(value, dict):
                raise ValueError("Slice A row must be an object")
            rows.append(value)
    if len(rows) != 83:
        raise ValueError(f"expected 83 frozen Slice A rows, got {len(rows)}")
    if sum(not bool(row.get("is_control")) for row in rows) != 82:
        raise ValueError("expected 82 eligible content rows")
    return rows


def _p90(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(0.90 * len(ordered)) - 1))
    return ordered[index]


def _write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for value in values:
            fh.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    ns = parser.parse_args()

    input_path = Path(ns.input)
    output_dir = Path(ns.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = _read_rows(input_path)
    client = OpenAICompatibleClient.from_env()

    output_rows: list[dict[str, Any]] = []
    unit_counts: list[int] = []
    nature = Counter()
    expression = Counter()
    status = Counter()
    totals = Counter()
    failures = 0

    for row in rows:
        base = {
            "stable_key": row.get("stable_key"),
            "source_version_id": row.get("source_version_id"),
            "event_time_raw": row.get("event_time_raw"),
            "source_seq": row.get("source_seq"),
            "sender": row.get("sender"),
            "raw_message": row.get("text"),
            "message_type": row.get("message_type"),
            "is_control": bool(row.get("is_control")),
            "control_kind": row.get("control_kind"),
            "evidence_refs": list(row.get("evidence_refs") or []),
            "relations": list(row.get("relations") or []),
        }
        if base["is_control"]:
            output_rows.append({**base, "shared_local_units": [], "semantic_execution": {"skipped_control": True}})
            continue
        try:
            result = extract_local_semantics(
                client,
                {
                    "original_text": row.get("text"),
                    "message_category": row.get("message_type"),
                },
                artifacts=(),
                scene_profile=None,
            )
        except Exception as exc:
            failures += 1
            output_rows.append({
                **base,
                "shared_local_units": [],
                "semantic_execution": {"error_type": type(exc).__name__},
            })
            continue

        units = [dict(unit) for unit in result.units]
        unit_counts.append(len(units))
        for unit in units:
            nature[str(unit.get("semantic_nature") or "unknown")] += 1
            expression[str(unit.get("expression") or "unknown")] += 1
            status[str(unit.get("status") or "unknown")] += 1
        totals["semantic_calls"] += int(result.discovery_calls)
        totals["prompt_tokens"] += int(result.prompt_tokens)
        totals["completion_tokens"] += int(result.completion_tokens)
        totals["total_tokens"] += int(result.total_tokens)
        totals["elapsed_ms"] += int(result.elapsed_ms)
        totals["retries"] += int(result.retry_count)
        output_rows.append({
            **base,
            "shared_local_units": units,
            "semantic_execution": {
                "model_id": result.model_id,
                "discovery_calls": result.discovery_calls,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "total_tokens": result.total_tokens,
                "elapsed_ms": result.elapsed_ms,
                "retry_count": result.retry_count,
            },
        })

    if failures:
        raise RuntimeError(f"semantic extraction failures={failures}")
    if len(unit_counts) != 82:
        raise RuntimeError(f"expected 82 eligible semantic results, got {len(unit_counts)}")

    probe_rows: list[dict[str, Any]] = []
    for probe_id, date_prefix, source_seq in PROBE_SPECS:
        matches = [
            row for row in output_rows
            if not row["is_control"]
            and str(row.get("event_time_raw") or "").startswith(date_prefix)
            and int(row.get("source_seq")) == source_seq
        ]
        if len(matches) != 1:
            raise RuntimeError(f"{probe_id}: expected one frozen probe row, got {len(matches)}")
        match = dict(matches[0])
        probe_rows.append({
            "probe_id": probe_id,
            "stable_key": match["stable_key"],
            "event_time_raw": match["event_time_raw"],
            "source_seq": match["source_seq"],
            "sender": match["sender"],
            "raw_message": match["raw_message"],
            "shared_local_units": match["shared_local_units"],
            "architecture_diagnostic": "PENDING_COMPANY_REVIEW",
        })

    summary = {
        "task_id": "COMPANY-SHARED-LOCAL-SEMANTICS-INTEGRATION-001",
        "diagnostic_role": "descriptive_integration_diagnostic_not_accuracy",
        "shared_source_sha": "cef2572dceddcab02f2cc18f35c9e4dbfe3469c8",
        "slice": {
            "slice_id": "A",
            "date_start": "2026-07-06",
            "date_end": "2026-07-07",
            "slice_messages": 83,
            "messages_processed": 83,
            "eligible_content_messages": 82,
            "control_provenance_messages": 1,
            "slice_b_opened": 0,
        },
        "semantics": {
            "messages_with_at_least_one_semantic_unit": sum(value > 0 for value in unit_counts),
            "messages_with_zero_unit": sum(value == 0 for value in unit_counts),
            "total_units": sum(unit_counts),
            "units_per_message_mean": round(statistics.mean(unit_counts), 6),
            "units_per_message_median": float(statistics.median(unit_counts)),
            "units_per_message_p90": _p90(unit_counts),
            "units_per_message_max": max(unit_counts, default=0),
            "semantic_nature_distribution": dict(sorted(nature.items())),
            "expression_distribution": dict(sorted(expression.items())),
            "status_distribution": dict(sorted(status.items())),
        },
        "execution": {
            "semantic_calls": totals["semantic_calls"],
            "prompt_tokens": totals["prompt_tokens"],
            "completion_tokens": totals["completion_tokens"],
            "total_tokens": totals["total_tokens"],
            "elapsed_ms": totals["elapsed_ms"],
            "retries": totals["retries"],
            "failures": failures,
            "attachment_body_fields_supplied": 0,
            "scene_profile_supplied": 0,
        },
        "probe_count": len(probe_rows),
        "metric_warning": "No independent local-semantic Gold exists for Slice A; these are descriptive statistics only.",
        "c1_policy_effect": "NONE; this diagnostic does not execute or alter C1 recall.",
    }

    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_jsonl(output_dir / "rows.jsonl", output_rows)
    _write_jsonl(output_dir / "probes.jsonl", probe_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
