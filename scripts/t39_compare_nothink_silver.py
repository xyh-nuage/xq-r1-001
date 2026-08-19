#!/usr/bin/env python3
from __future__ import annotations

import itertools
import json
from pathlib import Path
import sys

CORE = {"A1", "A2", "A3", "A6", "A8"}
EXCLUDED = {"A4", "U1"}


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: t39_compare_nothink_silver.py KNOWLEDGE_ROOT")
    root = Path(sys.argv[1])
    result_dir = root / "evaluation_results/full_context_human_cluster_nothink/2026-08-19_86929219_run32215945747"
    raw = json.loads((result_dir / "full_context_raw_result.json").read_text(encoding="utf-8"))
    sidecar = json.loads((result_dir / "human_context_map.json").read_text(encoding="utf-8"))
    v1 = json.loads((root / "context/evaluation/continuous_slice_a_matter_gold_redraft.v1.json").read_text(encoding="utf-8"))
    v2 = json.loads((root / "context/evaluation/continuous_slice_a_matter_gold_redraft.v2.json").read_text(encoding="utf-8"))

    parsed = raw.get("parsed_model_json") or {}
    clusters = parsed.get("clusters") or []
    ordinal_to_source = sidecar["ordinal_to_source"]
    stable_to_ordinal = {v["stable_id"]: int(k) for k, v in ordinal_to_source.items() if v.get("stable_id")}

    remap = ((v2.get("resolution_from_v1") or {}).get("group_remap") or {})
    overrides = {}
    for row in ((v2.get("resolution_from_v1") or {}).get("confidence_overrides") or []):
        if row.get("stable_key") and row.get("to"):
            overrides[row["stable_key"]] = row["to"]

    gold = {}
    for row in v1.get("assignments") or []:
        stable = row.get("stable_key")
        ordinal = stable_to_ordinal.get(stable)
        if ordinal is None:
            continue
        group = remap.get(row.get("group"), row.get("group"))
        confidence = overrides.get(stable, row.get("confidence"))
        gold[ordinal] = {"group": group, "confidence": confidence}
    if len(gold) != 83:
        raise AssertionError(f"expected 83 gold assignments, got {len(gold)}")

    pred_cluster_for = {}
    cluster_members = {}
    duplicates = []
    for cluster in clusters:
        cid = str(cluster.get("cluster"))
        members = [int(x) for x in cluster.get("messages") or []]
        cluster_members[cid] = members
        for o in members:
            if o in pred_cluster_for:
                duplicates.append(o)
            pred_cluster_for[o] = cid

    def eligible(conf: str, broad: bool) -> bool:
        return conf == "certain" or (broad and conf == "probable")

    def core_counts(members, broad: bool):
        out = {}
        for o in members:
            row = gold.get(o)
            if not row or row["group"] not in CORE or not eligible(row["confidence"], broad):
                continue
            out[row["group"]] = out.get(row["group"], 0) + 1
        return out

    mapping = {}
    per_cluster = {}
    for cid, members in cluster_members.items():
        broad_counts = core_counts(members, True)
        best = max(CORE, key=lambda g: (broad_counts.get(g, 0), g)) if broad_counts else None
        mapping[cid] = best
        per_cluster[cid] = {
            "predicted_message_count": len(members),
            "best_gold": best,
            "strict_core_counts": core_counts(members, False),
            "broad_core_counts": broad_counts,
            "excluded_or_noncore_count": sum(1 for o in members if gold[o]["group"] not in CORE or gold[o]["confidence"] == "provenance_only"),
        }

    def message_accuracy(broad: bool):
        candidates = [o for o, row in gold.items() if row["group"] in CORE and eligible(row["confidence"], broad)]
        correct = 0
        for o in candidates:
            cid = pred_cluster_for.get(o)
            if cid is not None and mapping.get(cid) == gold[o]["group"]:
                correct += 1
        return {"total": len(candidates), "correct": correct, "accuracy": round(correct / len(candidates), 4) if candidates else None}

    def pairwise(broad: bool):
        gold_members = {}
        for o, row in gold.items():
            if row["group"] in CORE and eligible(row["confidence"], broad):
                gold_members.setdefault(row["group"], set()).add(o)
        gold_pairs = set()
        for members in gold_members.values():
            gold_pairs.update(tuple(sorted(p)) for p in itertools.combinations(members, 2))
        pred_pairs = set()
        for members in cluster_members.values():
            pred_pairs.update(tuple(sorted(p)) for p in itertools.combinations(set(members), 2))
        tp = len(gold_pairs & pred_pairs)
        fp = len(pred_pairs - gold_pairs)
        fn = len(gold_pairs - pred_pairs)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        return {"tp": tp, "fp": fp, "fn": fn, "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}

    coverage = {}
    for group in sorted(CORE):
        broad_members = [o for o, row in gold.items() if row["group"] == group and eligible(row["confidence"], True)]
        strict_members = [o for o, row in gold.items() if row["group"] == group and eligible(row["confidence"], False)]
        best_cluster = max(cluster_members, key=lambda cid: sum(o in cluster_members[cid] for o in broad_members)) if cluster_members else None
        captured = sum(o in cluster_members.get(best_cluster, []) for o in broad_members) if best_cluster else 0
        coverage[group] = {
            "broad_total": len(broad_members),
            "best_cluster": best_cluster,
            "captured": captured,
            "recall": round(captured / len(broad_members), 4) if broad_members else None,
            "strict_total": len(strict_members),
            "strict_captured_by_best": sum(o in cluster_members.get(best_cluster, []) for o in strict_members) if best_cluster else 0,
        }

    by_gold_category = {}
    for group in ["A4", "U1", "A6"]:
        members = [o for o, row in gold.items() if row["group"] == group]
        by_gold_category[group] = {
            "ordinals": members,
            "clustered": [o for o in members if o in pred_cluster_for],
            "unresolved": [o for o in members if o in set(parsed.get("unresolved") or [])],
            "non_business": [o for o in members if o in set(parsed.get("non_business") or [])],
        }

    contamination = {}
    for cid, members in cluster_members.items():
        best = mapping.get(cid)
        bad = []
        for o in members:
            row = gold[o]
            if row["group"] != best:
                bad.append({"ordinal": o, "silver_group": row["group"], "confidence": row["confidence"]})
        contamination[cid] = bad

    report = {
        "source_result_commit": "39fe79682bc8385a829ae1ed6fd6b70dc1c100c1",
        "thinking_disabled": True,
        "finish_reason": raw.get("finish_reason"),
        "json_parse_ok": raw.get("parsed_model_json") is not None,
        "token_usage": raw.get("token_usage"),
        "cluster_count": len(clusters),
        "cluster_mapping": mapping,
        "per_cluster": per_cluster,
        "gold_coverage": coverage,
        "message_level_strict": message_accuracy(False),
        "message_level_broad": message_accuracy(True),
        "pairwise_strict": pairwise(False),
        "pairwise_broad": pairwise(True),
        "special_categories": by_gold_category,
        "contamination": contamination,
        "duplicate_pred_members_count": len(duplicates),
        "notes": [
            "A5 and A7 are remapped to A1 per v2.",
            "Strict uses certain only; broad uses certain+probable; provenance_only excluded.",
            "A4 and U1 are excluded from concrete Matter metrics but reported separately."
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
