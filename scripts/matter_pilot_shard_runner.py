#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT / "l1"))
sys.path.insert(0, str(ROOT / "l1" / "tools"))

import run_research_matter_reconstruction_pilot as r


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shared-replay", type=Path, required=True)
    ap.add_argument("--matter-gold", type=Path, required=True)
    ap.add_argument("--fixture-zip", type=Path, required=True)
    ap.add_argument("--speaker-report", type=Path, required=True)
    ap.add_argument("--business-report", type=Path, required=True)
    ap.add_argument("--public-key-b64", required=True)
    ap.add_argument("--shard-start", type=int, required=True)
    ap.add_argument("--shard-end", type=int, required=True)
    ap.add_argument("--encrypted-out", type=Path, required=True)
    ap.add_argument("--flat", type=Path, required=True)
    args = ap.parse_args()

    shared = r._load_jsonl(args.shared_replay)
    if len(shared) != 83:
        raise AssertionError(f"expected frozen 83-row Slice A, got {len(shared)}")
    messages_all = r._load_fixture(args.fixture_zip)
    by_key_all = {message.stable_key: message for message in messages_all}
    slice_keys = [str(row["stable_key"]) for row in shared]
    messages = tuple(by_key_all[key] for key in slice_keys)
    if len(messages) != 83 or len({message.group_id for message in messages}) != 1:
        raise AssertionError("frozen Slice A fixture alignment failed")

    shared_by = {str(row["stable_key"]): row for row in shared}
    research = {}
    for message in messages:
        row = shared_by[message.stable_key]
        if str(row.get("raw_message") or "") != str(message.original_text or ""):
            raise AssertionError(f"raw text mismatch: {message.stable_key}")
        research[message.stable_key] = r.adapt_shared_local_semantics(
            r._mapping(message), {"units": row.get("shared_local_units") or []}
        )

    # Candidate generation remains complete before Gold is loaded, exactly as the
    # private pilot runner does.
    pools, _structural_keys, _identifier_index = r._build_candidate_pools(messages, research)

    gold = r._load_json(args.matter_gold)
    certain, uncertain, matters = r._gold_membership(gold)
    by_key = {message.stable_key: message for message in messages}
    selected, features = r._select_seeds(
        slice_keys,
        by_key=by_key,
        research=research,
        pools=pools,
        certain=certain,
        uncertain=uncertain,
        matters=matters,
    )
    if len(selected) != 12:
        raise AssertionError(selected)
    if not (0 <= args.shard_start < args.shard_end <= len(selected)):
        raise AssertionError((args.shard_start, args.shard_end, len(selected)))

    group_names = {str(by_key[key].group_name or "") for key in selected}
    if len(group_names) != 1:
        raise AssertionError(group_names)
    group_name = next(iter(group_names))
    background = r.select_relevant_business_background(
        group_name=group_name,
        speaker_report=args.speaker_report.read_text(encoding="utf-8"),
        business_report=args.business_report.read_text(encoding="utf-8"),
    )
    if not background["group_speaker_context"] or not background["group_business_positioning_and_roles"]:
        raise AssertionError("deterministic business background selection failed")

    payloads = {}
    id_maps = {}
    for key in selected:
        payload, id_map = r.build_reconstruction_payload(
            source=r._message_source(by_key[key]),
            research_local_semantics=research[key],
            candidates=pools[key],
            background=background,
        )
        payload_text = json.dumps(payload, ensure_ascii=False).lower()
        for forbidden in ("matter_gold_id", "gold_membership", "gold_summary", "same_available", "cross_in_pool"):
            if forbidden in payload_text:
                raise AssertionError(f"Gold/selection leakage into model payload: {forbidden}")
        payloads[key] = payload
        id_maps[key] = id_map

    load_started = time.perf_counter()
    model = r.LocalQwenBackend()
    model_load_seconds = time.perf_counter() - load_started

    aggregate = Counter()
    details = []
    prov_total = 0
    prov_complete = 0
    schema_repairs = 0
    unsupported_refs = 0

    chosen = selected[args.shard_start:args.shard_end]
    for absolute_index, key in enumerate(chosen, start=args.shard_start + 1):
        parsed, raw_model_text, retry_count = model.complete_json(payloads[key])
        normalized = r.normalize_reconstruction_output(parsed, id_to_stable=id_maps[key])
        evaluation = r._evaluate_seed(
            key,
            normalized,
            pools[key],
            certain=certain,
            uncertain=uncertain,
            matters=matters,
        )
        p_total, p_complete = r._provenance_item_counts(normalized)
        prov_total += p_total
        prov_complete += p_complete
        schema_repairs += int(normalized["normalization"]["schema_repair_count"])
        unsupported_refs += int(normalized["normalization"]["unsupported_evidence_ref_count"])
        aggregate["gold_same_total"] += evaluation["gold_same_total"]
        aggregate["gold_same_available"] += evaluation["gold_same_available"]
        aggregate["tp"] += evaluation["supporting_true_positive"]
        aggregate["cross_false"] += evaluation["cross_matter_false_inclusion"]
        aggregate["gold_available_rejected"] += evaluation["gold_available_but_rejected"]
        aggregate["uncertain_decisions"] += evaluation["uncertain_decision_count"]
        aggregate["support_unknown"] += evaluation["supporting_gold_uncertain_or_unassigned"]
        details.append({
            "seed_index": absolute_index,
            "stable_key": key,
            "selection_features": features[key],
            "source": r._message_source(by_key[key]),
            "research_local_semantics": research[key],
            "candidate_pool": pools[key],
            "background_used": background,
            "model_payload": payloads[key],
            "model_raw_output": raw_model_text,
            "model_retry_count": retry_count,
            "model_output": normalized,
            "gold_evaluation": evaluation,
        })

    detail = {
        "task_id": "RESEARCH-MATTER-RECONSTRUCTION-PILOT-001",
        "source_sha_contract": "exact-private-source",
        "selected_seed_stable_keys": selected,
        "shard_start": args.shard_start,
        "shard_end": args.shard_end,
        "seed_details": details,
        "runtime": {
            "model": r.MODEL_ID,
            "model_load_seconds": model_load_seconds,
            "model_calls": model.calls,
            "model_retries": model.retries,
            "generated_tokens": model.generated_tokens,
            "generation_seconds": model.elapsed_seconds,
        },
    }
    encrypted = r._encrypt_detail(detail, args.public_key_b64)
    args.encrypted_out.parent.mkdir(parents=True, exist_ok=True)
    args.encrypted_out.write_text(encrypted, encoding="ascii")

    evaluable_support = aggregate["tp"] + aggregate["cross_false"]
    flat = {
        "seed_count": len(chosen),
        "shard_start": args.shard_start,
        "shard_end": args.shard_end,
        "gold_same_total": aggregate["gold_same_total"],
        "gold_same_available": aggregate["gold_same_available"],
        "supporting_true_positive": aggregate["tp"],
        "supporting_precision": aggregate["tp"] / evaluable_support if evaluable_support else 1.0,
        "supporting_recall": aggregate["tp"] / aggregate["gold_same_available"] if aggregate["gold_same_available"] else 1.0,
        "cross_false_inclusion": aggregate["cross_false"],
        "gold_available_rejected": aggregate["gold_available_rejected"],
        "uncertain_count": aggregate["uncertain_decisions"],
        "support_unknown": aggregate["support_unknown"],
        "provenance_items_total": prov_total,
        "provenance_items_complete": prov_complete,
        "schema_repair_count": schema_repairs,
        "unsupported_evidence_ref_count": unsupported_refs,
        "model_calls": model.calls,
        "model_retries": model.retries,
    }
    args.flat.parent.mkdir(parents=True, exist_ok=True)
    args.flat.write_text(json.dumps(flat, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PILOT_SHARD_ENCRYPTED_DETAIL_READY=1")


if __name__ == "__main__":
    main()
