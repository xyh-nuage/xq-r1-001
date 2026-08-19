#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: t48_probe_first48_file_names_only_packet.py SOURCE_ROOT OUTPUT_DIR")
    root = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root / "l1" / "tools"))
    sys.path.insert(0, str(root / "l1"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    import run_research_full_context_matter_baseline as baseline
    import build_research_full_context_human_packet_simple_thinking as human
    import t48_shangtang_first48_file_names_only_low3000_retry429 as t48

    original = baseline.build_full_context_input(
        shared_replay=root / "context/evaluation/company_shared_local_semantics_slice_a.jsonl",
        fixture_zip=root / "l1/tests/fixtures/obsolete_media_test_data.zip",
        l1_candidate_db=root / "l1/evaluation/vnext/runtime/full_context_matter_input/l1_candidate_state.db",
        l1_run_id="run_c46e10f6833adb71",
    )
    model_input, filter_stats = t48.sanitize_first48(original)
    baseline._write_json(out / "full_context_input.json", model_input)
    packet, sidecar, stats = human.build_human_packet(model_input)
    stats = dict(stats)
    stats.update(filter_stats)
    stats["provider_call_count"] = 0

    checks = {}
    checks["message_count_48"] = stats.get("message_count") == 48 and stats.get("model_message_count") == 48
    checks["suppressed_file_messages_ge_5"] = stats.get("suppressed_file_messages", 0) >= 5
    checks["suppressed_file_chars_ge_1500"] = stats.get("suppressed_file_preview_chars_first48", 0) >= 1500
    checks["no_message49"] = "消息49 |" not in packet
    checks["phone_kept"] = "13608997076" in packet
    checks["id_kept"] = "372824196909060636" in packet
    checks["file_body_markers_absent"] = all(marker not in packet for marker in t48.KNOWN_FILE_CONTENT_MARKERS)
    checks["required_file_names_present"] = all(name in packet for name in t48.REQUIRED_FILE_NAMES)

    (out / "human_context_packet.txt").write_text(packet, encoding="utf-8")
    (out / "human_context_system_prompt.txt").write_text(human.SYSTEM_PROMPT + "\n", encoding="utf-8")
    write_json(out / "human_context_map.json", sidecar)
    write_json(out / "human_context_stats.json", stats)
    write_json(out / "packet_gate_checks.json", checks)

    print("PACKET_PROBE_DONE")
    for key, value in checks.items():
        print(f"check_{key}={str(value).lower()}")
    print("packet_chars=" + str(len(packet)))
    print("raw_attachment_preview_chars=" + str(stats.get("raw_attachment_preview_chars_first48")))
    print("kept_attachment_preview_chars=" + str(stats.get("kept_attachment_preview_chars_first48")))
    print("removed_attachment_preview_chars=" + str(stats.get("removed_attachment_preview_chars_first48")))
    print("suppressed_file_preview_chars=" + str(stats.get("suppressed_file_preview_chars_first48")))
    print("suppressed_file_messages=" + str(stats.get("suppressed_file_messages")))
    print("attachment_preview_chars_model_facing=" + str(stats.get("attachment_preview_chars")))
    print("provider_call_count=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
