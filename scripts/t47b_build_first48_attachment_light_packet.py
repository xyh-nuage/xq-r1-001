#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: t47b_build_first48_attachment_light_packet.py SOURCE_ROOT OUTPUT_DIR")
    root = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root / "l1" / "tools"))
    sys.path.insert(0, str(root / "l1"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    import run_research_full_context_matter_baseline as baseline
    import build_research_full_context_human_packet_simple_thinking as human
    import t47_shangtang_first48_attachment_light_low3000_retry429 as t47

    original = baseline.build_full_context_input(
        shared_replay=root / "context/evaluation/company_shared_local_semantics_slice_a.jsonl",
        fixture_zip=root / "l1/tests/fixtures/obsolete_media_test_data.zip",
        l1_candidate_db=root / "l1/evaluation/vnext/runtime/full_context_matter_input/l1_candidate_state.db",
        l1_run_id="run_c46e10f683adb71",
    )
    if (original.get("user_payload") or {}).get("mature_matters"):
        raise SystemExit("mature_matters must be empty")

    doc = copy.deepcopy(original)
    payload = doc.get("user_payload") or {}
    messages = list(payload.get("messages") or [])[:48]
    payload["messages"] = messages
    doc["user_payload"] = payload

    raw_preview_chars = 0
    kept_preview_chars = 0
    suppressed_file_previews = 0
    trimmed_image_previews = 0

    for message in messages:
        text = str((message or {}).get("original_text") or "").lstrip()
        first_line = text.splitlines()[0].strip() if text.splitlines() else ""
        is_file_message = first_line == "文件"
        for attachment in list((message or {}).get("attachments") or []):
            parsed = list((attachment or {}).get("parsed_content") or [])
            before_chars = sum(
                len(str(row.get("preview_content") or ""))
                for row in parsed if isinstance(row, dict)
            )
            raw_preview_chars += before_chars

            if is_file_message:
                if before_chars:
                    suppressed_file_previews += 1
                attachment["parsed_content"] = []
                continue

            new_rows = []
            changed = False
            for row in parsed:
                if not isinstance(row, dict):
                    continue
                new_row = dict(row)
                before = str(new_row.get("preview_content") or "")
                after = t47.trim_image_preview(before)
                new_row["preview_content"] = after
                if after != before.strip():
                    changed = True
                if after:
                    kept_preview_chars += len(after)
                    new_rows.append(new_row)
            if changed:
                trimmed_image_previews += 1
            attachment["parsed_content"] = new_rows

    baseline._write_json(out / "full_context_input.json", doc)
    packet, sidecar, stats = human.build_human_packet(doc)
    stats = dict(stats)
    stats.update({
        "model_message_count": len(messages),
        "raw_attachment_preview_chars_first48": raw_preview_chars,
        "kept_attachment_preview_chars_first48": kept_preview_chars,
        "removed_attachment_preview_chars_first48": raw_preview_chars - kept_preview_chars,
        "suppressed_file_previews": suppressed_file_previews,
        "trimmed_image_previews": trimmed_image_previews,
        "chat_original_text_modified": False,
        "file_detection": "first original_text line equals 文件",
        "provider_call_count": 0,
    })
    if stats.get("message_count") != 48 or "消息49 |" in packet:
        raise SystemExit("packet boundary check failed")

    (out / "human_context_packet.txt").write_text(packet, encoding="utf-8")
    (out / "human_context_system_prompt.txt").write_text(human.SYSTEM_PROMPT + "\n", encoding="utf-8")
    write_json(out / "human_context_map.json", sidecar)
    write_json(out / "human_context_stats.json", stats)

    print("PACKET_BUILD_OK")
    print("model_message_count=48")
    print("packet_chars=" + str(len(packet)))
    print("attachment_preview_chars=" + str(stats.get("attachment_preview_chars")))
    print("raw_attachment_preview_chars=" + str(raw_preview_chars))
    print("removed_attachment_preview_chars=" + str(raw_preview_chars - kept_preview_chars))
    print("suppressed_file_previews=" + str(suppressed_file_previews))
    print("trimmed_image_previews=" + str(trimmed_image_previews))
    print("provider_call_count=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
