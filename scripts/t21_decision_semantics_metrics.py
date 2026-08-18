#!/usr/bin/env python3
"""Emit private-input-safe numeric aggregates for C-stage decision semantics."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _need_type(value: Any) -> str:
    return str(value.get("need_type") or "") if isinstance(value, Mapping) else ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transition-log", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    rows = _load_jsonl(Path(args.transition_log))
    metrics: dict[str, int] = {
        "rows": len(rows),
        "first_unresolved_total": 0,
        "first_unresolved_no_evidence_need": 0,
        "first_unresolved_historical_chat_need": 0,
        "first_unresolved_attachment_need": 0,
        "first_unresolved_other_need": 0,
        "final_unresolved_total": 0,
        "final_unresolved_no_evidence_need": 0,
        "final_unresolved_historical_chat_need": 0,
        "final_unresolved_attachment_need": 0,
        "final_unresolved_other_need": 0,
        "c1_triggered_sources": 0,
        "new_gate_rejection_sources": 0,
        "new_gate_rejection_calls": 0,
        "new_gate_rejection_sources_with_immediate_c1": 0,
        "new_rejection_synthesized_historical_chat_count": 0,
        "weak_model_resolution_rejected_count": 0,
        "model_strong_resolution_accepted_count": 0,
        "system_direct_unresolved_resolution_count": 0,
        "system_quote_unresolved_resolution_count": 0,
        "system_identifier_unresolved_resolution_count": 0,
        "accepted_new_single_verified_foundation": 0,
        "accepted_new_multiple_verified_foundation": 0,
    }

    for row in rows:
        first_decision = str(row.get("first_decision") or "")
        first_need = row.get("first_evidence_need")
        if first_decision == "UNRESOLVED":
            metrics["first_unresolved_total"] += 1
            need = _need_type(first_need)
            if not need:
                metrics["first_unresolved_no_evidence_need"] += 1
            elif need == "HISTORICAL_CHAT":
                metrics["first_unresolved_historical_chat_need"] += 1
            elif need == "ATTACHMENT_CONTENT":
                metrics["first_unresolved_attachment_need"] += 1
            else:
                metrics["first_unresolved_other_need"] += 1

        final_decision = str(row.get("final_decision") or "")
        final_need = row.get("final_evidence_need")
        if final_decision == "UNRESOLVED":
            metrics["final_unresolved_total"] += 1
            need = _need_type(final_need)
            if not need:
                metrics["final_unresolved_no_evidence_need"] += 1
            elif need == "HISTORICAL_CHAT":
                metrics["final_unresolved_historical_chat_need"] += 1
            elif need == "ATTACHMENT_CONTENT":
                metrics["final_unresolved_attachment_need"] += 1
            else:
                metrics["final_unresolved_other_need"] += 1

        c1_triggered = bool(row.get("c1_triggered"))
        if c1_triggered:
            metrics["c1_triggered_sources"] += 1

        source_new_rejected = False
        for audit_key in ("first_call_audit", "second_call_audit"):
            audit = row.get(audit_key)
            if not isinstance(audit, Mapping):
                continue
            normalization = audit.get("normalization") if isinstance(audit.get("normalization"), Mapping) else {}
            rejected = int(normalization.get("new_downgraded_to_unresolved_count") or 0)
            if rejected:
                source_new_rejected = True
                metrics["new_gate_rejection_calls"] += rejected
            metrics["new_rejection_synthesized_historical_chat_count"] += int(
                normalization.get("new_rejection_synthesized_historical_chat_count") or 0
            )
            metrics["weak_model_resolution_rejected_count"] += int(
                normalization.get("weak_model_resolution_rejected_count") or 0
            )
            metrics["system_direct_unresolved_resolution_count"] += int(
                normalization.get("system_unresolved_resolution_count") or 0
            )
            metrics["system_quote_unresolved_resolution_count"] += int(
                normalization.get("system_quote_unresolved_resolution_count") or 0
            )
            metrics["system_identifier_unresolved_resolution_count"] += int(
                normalization.get("system_identifier_unresolved_resolution_count") or 0
            )

            lifecycle = audit.get("unresolved_lifecycle_verification") if isinstance(audit.get("unresolved_lifecycle_verification"), Mapping) else {}
            metrics["model_strong_resolution_accepted_count"] += len([
                item for item in lifecycle.get("verified_resolutions") or []
                if isinstance(item, Mapping) and isinstance(item.get("strong_resolution_proof"), Mapping)
            ])

        if source_new_rejected:
            metrics["new_gate_rejection_sources"] += 1
            if c1_triggered:
                metrics["new_gate_rejection_sources_with_immediate_c1"] += 1

        if final_decision == "NEW_MATTER":
            final_audit = row.get("second_call_audit") if isinstance(row.get("second_call_audit"), Mapping) else row.get("first_call_audit")
            if isinstance(final_audit, Mapping):
                verification = final_audit.get("new_matter_verification") if isinstance(final_audit.get("new_matter_verification"), Mapping) else {}
                count = len([x for x in verification.get("verified_foundation") or [] if isinstance(x, Mapping)])
                if count == 1:
                    metrics["accepted_new_single_verified_foundation"] += 1
                elif count > 1:
                    metrics["accepted_new_multiple_verified_foundation"] += 1

    Path(args.output).write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print("decision_semantics_numeric_audit=PASS")


if __name__ == "__main__":
    main()
