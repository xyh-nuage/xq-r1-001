#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

PROVIDER_BASE_URL = "https://token.sensenova.cn/v1/"
MAX_TOKENS = 8000
REASONING_EFFORT = "low"
BACKOFF_SECONDS = [60, 120, 240]
GLOBAL_OFFSET = 48


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def remap_packet_ordinals(packet: str) -> str:
    out: list[str] = []
    for line in packet.splitlines():
        m = re.match(r"^消息(\d+) \|", line)
        if m:
            local = int(m.group(1))
            line = f"消息{local + GLOBAL_OFFSET} |" + line[m.end():]
        elif line.lstrip().startswith("↩"):
            line = re.sub(r"消息(\d+)", lambda x: f"消息{int(x.group(1)) + GLOBAL_OFFSET}", line)
        out.append(line)
    return "\n".join(out).rstrip() + "\n"


def remap_sidecar(sidecar: dict) -> dict:
    mapped = copy.deepcopy(sidecar)
    old = dict(mapped.get("ordinal_to_source") or {})
    mapped["ordinal_to_source"] = {
        str(int(k) + GLOBAL_OFFSET): v for k, v in old.items()
    }
    for item in list(mapped.get("attachments") or []):
        if isinstance(item, dict) and item.get("first_message_ordinal") is not None:
            item["first_message_ordinal"] = int(item["first_message_ordinal"]) + GLOBAL_OFFSET
    mapped["model_visible_ordinal_range"] = [49, 83]
    mapped["source_window"] = "original messages 49-83, rendered as 49-83"
    return mapped


def sanitize_second35(input_doc: dict, t48) -> tuple[dict, dict]:
    doc = copy.deepcopy(input_doc)
    payload = doc.get("user_payload") or {}
    original_messages = list(payload.get("messages") or [])
    if len(original_messages) != 83:
        raise AssertionError(f"expected frozen 83-message source, got {len(original_messages)}")
    messages = original_messages[48:83]
    payload["messages"] = messages
    doc["user_payload"] = payload

    raw_preview_chars = 0
    kept_preview_chars = 0
    suppressed_file_preview_chars = 0
    suppressed_file_messages = 0
    trimmed_image_previews = 0
    original_text_snapshot = [str((m or {}).get("original_text") or "") for m in messages]

    for message in messages:
        original_text = str((message or {}).get("original_text") or "")
        file_message = t48.is_file_message(original_text)
        if file_message:
            suppressed_file_messages += 1

        for attachment in list((message or {}).get("attachments") or []):
            parsed = list((attachment or {}).get("parsed_content") or [])
            preview_chars_here = sum(
                len(str(item.get("preview_content") or ""))
                for item in parsed if isinstance(item, dict)
            )
            raw_preview_chars += preview_chars_here

            if file_message:
                suppressed_file_preview_chars += preview_chars_here
                attachment["parsed_content"] = []
                continue

            changed = False
            new_rows = []
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                new_item = dict(item)
                before = str(new_item.get("preview_content") or "").strip()
                after = t48.trim_image_preview(before)
                if after != before:
                    changed = True
                if after:
                    new_item["preview_content"] = after
                    kept_preview_chars += len(after)
                    new_rows.append(new_item)
            if changed:
                trimmed_image_previews += 1
            attachment["parsed_content"] = new_rows

    if [str((m or {}).get("original_text") or "") for m in messages] != original_text_snapshot:
        raise AssertionError("chat original_text was modified")

    for message in messages:
        if t48.is_file_message(str((message or {}).get("original_text") or "")):
            for attachment in list((message or {}).get("attachments") or []):
                if list((attachment or {}).get("parsed_content") or []):
                    raise AssertionError("file message still has model-facing parsed_content")

    stats = {
        "model_message_count": len(messages),
        "source_global_ordinal_start": 49,
        "source_global_ordinal_end": 83,
        "raw_attachment_preview_chars_second35": raw_preview_chars,
        "kept_attachment_preview_chars_second35": kept_preview_chars,
        "removed_attachment_preview_chars_second35": raw_preview_chars - kept_preview_chars,
        "suppressed_file_preview_chars_second35": suppressed_file_preview_chars,
        "suppressed_file_messages": suppressed_file_messages,
        "trimmed_image_previews": trimmed_image_previews,
        "chat_original_text_modified": False,
        "file_policy": "file message keeps original chat/file name; parsed attachment preview suppressed",
        "image_policy": "OCR/table preserved; obvious camera/environment prose removed",
    }
    return doc, stats


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: t56_shangtang_second35_low8000_backoff.py SOURCE_ROOT OUTPUT_DIR")

    root = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root / "l1" / "tools"))
    sys.path.insert(0, str(root / "l1"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    import run_research_full_context_matter_baseline as baseline
    import build_research_full_context_human_packet_simple_thinking as human
    import t48_shangtang_first48_file_names_only_low3000_retry429 as t48

    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")
    source_sha = os.environ.get("SOURCE_SHA", "")
    if not all([api_key, model, source_sha]):
        raise SystemExit("missing required environment")

    original = baseline.build_full_context_input(
        shared_replay=root / "context/evaluation/company_shared_local_semantics_slice_a.jsonl",
        fixture_zip=root / "l1/tests/fixtures/obsolete_media_test_data.zip",
        l1_candidate_db=root / "l1/evaluation/vnext/runtime/full_context_matter_input/l1_candidate_state.db",
        l1_run_id="run_c46e10f6833adb71",
    )
    if (original.get("user_payload") or {}).get("mature_matters"):
        raise SystemExit("mature_matters must be empty")

    model_input, filter_stats = sanitize_second35(original, t48)
    baseline._write_json(out / "full_context_input.json", model_input)
    packet_local, sidecar, stats = human.build_human_packet(model_input)
    packet = remap_packet_ordinals(packet_local)
    sidecar = remap_sidecar(sidecar)
    stats = dict(stats)
    stats.update(filter_stats)
    stats["packet_chars"] = len(packet)
    stats["packet_bytes"] = len(packet.encode("utf-8"))

    # Pre-provider gates. No Gold/Silver/reference is opened here.
    headers = [int(x) for x in re.findall(r"(?m)^消息(\d+) \|", packet)]
    if headers != list(range(49, 84)):
        raise SystemExit(f"global message headers incorrect: {headers}")
    if stats.get("message_count") != 35 or stats.get("model_message_count") != 35:
        raise SystemExit("second35 packet must contain exactly 35 messages")
    if "消息48 |" in packet or "消息84 |" in packet:
        raise SystemExit("second35 packet escaped source window")

    (out / "human_context_packet.txt").write_text(packet, encoding="utf-8")
    (out / "human_context_system_prompt.txt").write_text(human.SYSTEM_PROMPT + "\n", encoding="utf-8")
    write_json(out / "human_context_map.json", sidecar)
    write_json(out / "human_context_stats.json", stats)

    print("PACKET_GATE_PASS")
    print("model_message_count=35")
    print("visible_message_range=49-83")
    print("packet_chars=" + str(len(packet)))
    print("attachment_preview_chars=" + str(stats.get("attachment_preview_chars")))
    print("raw_attachment_preview_chars_second35=" + str(stats.get("raw_attachment_preview_chars_second35")))
    print("kept_attachment_preview_chars_second35=" + str(stats.get("kept_attachment_preview_chars_second35")))
    print("suppressed_file_preview_chars_second35=" + str(stats.get("suppressed_file_preview_chars_second35")))
    print("suppressed_file_messages=" + str(stats.get("suppressed_file_messages")))

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": human.SYSTEM_PROMPT},
            {"role": "user", "content": packet},
        ],
        "thinking": {"type": "enabled"},
        "reasoning_effort": REASONING_EFFORT,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    request_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    url = PROVIDER_BASE_URL.rstrip("/") + "/chat/completions"
    attempts = []
    envelope = None
    status = None
    started = time.monotonic()

    max_attempts = len(BACKOFF_SECONDS) + 1
    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(url, data=request_bytes, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as response:
                status = int(response.status)
                raw_bytes = response.read()
            envelope = json.loads(raw_bytes.decode("utf-8", errors="replace"))
            attempts.append({"attempt": attempt, "http_status": status, "result": "success"})
            break
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")[:4000]
            attempts.append({"attempt": attempt, "http_status": int(exc.code), "result": "http_error", "body": err_body})
            if int(exc.code) == 429 and attempt < max_attempts:
                delay = BACKOFF_SECONDS[attempt - 1]
                print(f"HTTP_429_BACKOFF_SECONDS={delay}")
                time.sleep(delay)
                continue
            write_json(out / "full_context_raw_result.json", {
                "schema_version": "research_second35_low8000_raw_result.v1",
                "status": "MODEL_CALL_FAILED_PRE_REFERENCE",
                "exact_source_commit": source_sha,
                "provider": urlparse(PROVIDER_BASE_URL).netloc,
                "configured_model": model,
                "thinking": {"type": "enabled"},
                "reasoning_effort": REASONING_EFFORT,
                "max_tokens": MAX_TOKENS,
                "packet_stats": stats,
                "request_bytes": len(request_bytes),
                "model_call_count": len(attempts),
                "retry_count": max(0, len(attempts) - 1),
                "backoff_seconds": BACKOFF_SECONDS,
                "attempts": attempts,
                "silver_gold_reference_opened_before_this_file": False,
            })
            print(f"TERMINAL_STATUS=HTTP_{int(exc.code)}")
            print("provider_attempts=" + str(len(attempts)))
            return 20

    elapsed_ms = int((time.monotonic() - started) * 1000)
    payload = envelope.get("data") if isinstance(envelope, dict) and isinstance(envelope.get("data"), dict) else envelope
    usage = payload.get("usage") if isinstance(payload, dict) and isinstance(payload.get("usage"), dict) else {}
    choices = payload.get("choices") if isinstance(payload, dict) else []
    first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first, dict) else {}
    content = message.get("content") if isinstance(message, dict) else None
    reasoning = message.get("reasoning_content") if isinstance(message, dict) else None
    finish_reason = first.get("finish_reason") if isinstance(first, dict) else None

    parsed = None
    parse_error = None
    if isinstance(content, str) and content:
        try:
            parsed = json.loads(content)
        except Exception as exc:
            parse_error = f"{type(exc).__name__}: {exc}"
    else:
        parse_error = "response content is empty or not text"

    raw = {
        "schema_version": "research_second35_low8000_raw_result.v1",
        "status": "MODEL_HTTP_SUCCESS_PRE_REFERENCE",
        "exact_source_commit": source_sha,
        "provider": urlparse(PROVIDER_BASE_URL).netloc,
        "configured_model": model,
        "response_model_version": str(payload.get("model") or model) if isinstance(payload, dict) else model,
        "http_status": status,
        "thinking": {"type": "enabled"},
        "reasoning_effort": REASONING_EFFORT,
        "max_tokens": MAX_TOKENS,
        "packet_stats": stats,
        "request_bytes": len(request_bytes),
        "model_call_count": len(attempts),
        "retry_count": max(0, len(attempts) - 1),
        "backoff_seconds": BACKOFF_SECONDS,
        "attempts": attempts,
        "elapsed_ms": elapsed_ms,
        "token_usage": usage,
        "finish_reason": finish_reason,
        "raw_model_content": content,
        "parsed_model_json": parsed,
        "parse_error": parse_error,
        "raw_reasoning_content": reasoning,
        "provider_envelope": envelope,
        "silver_gold_reference_opened_before_this_file": False,
    }
    write_json(out / "full_context_raw_result.json", raw)

    details = usage.get("completion_tokens_details") or {}
    print("TERMINAL_STATUS=HTTP_200")
    print("provider_attempts=" + str(len(attempts)))
    print("retry_count=" + str(max(0, len(attempts) - 1)))
    print("model_message_count=35")
    print("visible_message_range=49-83")
    print("reasoning_effort=low")
    print("max_tokens=8000")
    print("request_bytes=" + str(len(request_bytes)))
    print("packet_chars=" + str(len(packet)))
    print("prompt_tokens=" + str(usage.get("prompt_tokens") or usage.get("input_tokens") or ""))
    print("completion_tokens=" + str(usage.get("completion_tokens") or usage.get("output_tokens") or ""))
    print("reasoning_tokens=" + str(details.get("reasoning_tokens") if isinstance(details, dict) else ""))
    print("total_tokens=" + str(usage.get("total_tokens") or ""))
    print("finish_reason=" + str(finish_reason or ""))
    print("json_parse_ok=" + str(parsed is not None).lower())
    print("content_chars=" + str(len(content) if isinstance(content, str) else -1))
    if isinstance(content, str) and content:
        print("CONTENT_BEGIN")
        print(content)
        print("CONTENT_END")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
