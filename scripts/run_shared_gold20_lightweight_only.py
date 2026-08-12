#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
from typing import Any

from l1_processor.candidate.catalog import SceneProfileRegistry
from l1_processor.candidate_source import CandidateL0Source
from l1_processor.llm_client import OpenAICompatibleClient
from l1_processor.local_semantics import extract_local_semantics


def load_evaluator(path: Path):
    spec = importlib.util.spec_from_file_location("shared_gold20_evaluator", path)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load committed evaluator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Shared-only reviewed Gold20 diagnostic")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--evaluator", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    evaluator = load_evaluator(args.evaluator)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    records = list(manifest.get("records") or [])
    if len(records) != 20:
        raise SystemExit(f"expected exactly 20 reviewed sources, got {len(records)}")
    gold_by_position = {int(case["position"]): case for case in gold.get("cases") or []}
    if set(gold_by_position) != set(range(1, 21)):
        raise SystemExit("Gold positions must be exactly 1..20")

    registry = SceneProfileRegistry.load(args.profiles)
    client = OpenAICompatibleClient.from_env()
    predictions_by_position: dict[int, list[dict[str, Any]]] = {}
    prediction_rows: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    cost_rows: list[dict[str, int]] = []
    case_details: list[dict[str, Any]] = []

    with CandidateL0Source(args.db) as source:
        database_fingerprint = source.compute_fingerprint()
        for position, record in enumerate(records, 1):
            stable_key = str(record.get("source_stable_key") or "")
            if not stable_key:
                raise SystemExit(f"manifest position {position} has no source_stable_key")
            message = evaluator.message_for(source, stable_key)
            artifacts = evaluator.artifacts_for(source, str(message["source_version_id"]))
            profile = registry.profile_for(str(message.get("group_id") or ""))
            scene_profile = profile.to_prompt_dict("priorities")

            result = extract_local_semantics(
                client,
                message,
                artifacts=artifacts,
                scene_profile=scene_profile,
            )
            units = [evaluator._project_local(unit) for unit in result.units]
            stats = evaluator._stats_from_local(result)
            predictions_by_position[position] = units
            cost_rows.append(stats)
            prediction_rows.append(
                {
                    "position": position,
                    "units": units,
                    "model": stats,
                    "artifact_count": len(artifacts),
                }
            )
            source_rows.append(
                {
                    "position": position,
                    "manifest_record": record,
                    "message": message,
                    "artifacts": artifacts,
                    "scene_profile": scene_profile,
                }
            )

            selected = evaluator._select_local_gold_variant(gold_by_position[position], units)
            expected = list(selected["expected_units"])
            pairs = [list(pair) for pair in selected["pairs"]]
            matched_expected = {int(pair[0]) for pair in pairs}
            matched_actual = {int(pair[1]) for pair in pairs}
            case_details.append(
                {
                    "position": position,
                    "variant_id": selected["variant_id"],
                    "expected_units": expected,
                    "actual_units": units,
                    "matched_pairs": pairs,
                    "missing_expected_units": [
                        expected[index]
                        for index in range(len(expected))
                        if index not in matched_expected
                    ],
                    "extra_actual_units": [
                        units[index]
                        for index in range(len(units))
                        if index not in matched_actual
                    ],
                }
            )

    evaluation = evaluator._evaluate_pipeline(gold_by_position, predictions_by_position)
    cost = evaluator._aggregate_cost(cost_rows)
    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    with (output / "lightweight_predictions.jsonl").open("w", encoding="utf-8") as fh:
        for row in prediction_rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    with (output / "source_snapshots.jsonl").open("w", encoding="utf-8") as fh:
        for row in source_rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    diagnostic = {
        "schema_version": "shared_gold20_lightweight_diagnostic.v1",
        "source_sha": source_sha,
        "database_fingerprint": database_fingerprint,
        "sources": 20,
        "reviewed_gold_units_primary_variants": 28,
        "gold_supported_lightweight": evaluation,
        "operational_cost": cost,
        "cases": case_details,
    }
    dump_json(output / "shared_only_diagnostic.json", diagnostic)

    summary = {
        "sources": 20,
        "reviewed_gold_units_primary_variants": 28,
        "predicted_units": evaluation["predicted_units"],
        "unit_precision": evaluation["unit_precision"],
        "unit_recall": evaluation["unit_recall"],
        "unit_f1": evaluation["unit_f1"],
        "nature_accuracy": evaluation["nature_accuracy"],
        "expression_accuracy": evaluation["expression_accuracy"],
        "false_no_output": evaluation["false_no_output"],
        "extra_units": evaluation["extra_units"],
        "missing_units": evaluation["missing_units"],
        "calls": int(cost["totals"].get("calls", 0)),
        "prompt_tokens": int(cost["totals"].get("prompt_tokens", 0)),
        "completion_tokens": int(cost["totals"].get("completion_tokens", 0)),
        "total_tokens": int(cost["totals"].get("total_tokens", 0)),
        "elapsed_ms": int(cost["totals"].get("elapsed_ms", 0)),
        "retries": int(cost["totals"].get("retries", 0)),
    }
    dump_json(output / "shared_only_summary.json", summary)


if __name__ == "__main__":
    main()
