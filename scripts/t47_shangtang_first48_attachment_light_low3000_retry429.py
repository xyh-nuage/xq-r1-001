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
MAX_TOKENS = 3000
REASONING_EFFORT = "low"
RETRY_429_DELAY_SECONDS = 60
FILE_EXTENSIONS = {".pdf", ".xlsx", ".xls", ".doc", ".docx", ".ppt", ".pptx", ".csv", ".txt", ".zip"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
BUSINESS_TERMS = (
    "车牌", "车号", "货物", "木薯", "淀粉", "装载", "提单", "合同", "订单", "司机", "姓名",
    "电话", "身份证", "地址", "日期", "时间", "称重", "净重", "毛重", "重量", "吨", "kg", "KG",
    "签收", "签名", "收货", "发货", "托盘", "包", "柜", "批次", "编号", "品牌", "产品"
)
NOISE_TERMS = (
    "vivo", "AURA LIGHT", "ISO", "f/", "相机水印", "木纹桌面", "天花板", "混凝土地面", "金属瓦楞",
    "低角度", "亮红橙色", "红色涂装", "白蓝相间", "仓库内部", "背景中", "叉车", "车轮", "梁柱", "支撑结构"
)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def is_table_like(text: str) -> bool:
    return text.count("|") >= 6 or text.count("\n-") >= 2


def trim_image_preview(text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    if is_table_like(text):
        lines = [line for line in text.splitlines() if not any(term in line for term in NOISE_TERMS)]
        return "\n".join(lines).strip()

    pieces = []
    for raw_line in text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        for part in re.split(r"(?<=[。！？；])", raw_line):
            part = part.strip()
            if not part:
                continue
            if any(term in part for term in BUSINESS_TERMS):
                pieces.append(part)
    return "\n".join(pieces).strip()


def sanitize_first48(input_doc: dict) -> tuple[dict, dict]:
    doc = copy.deepcopy(input_doc)
    payload = doc.get("user_payload") or {}
    messages = list(payload.get("messages") or [])[:48]
    payload["messages"] = messages
    doc["user_payload"] = payload

    raw_preview_chars = 0
    kept_preview_chars = 0
    suppressed_file_previews = 0
    trimmed_image_previews = 0

    for message in messages:
        for attachment in list((message or {}).get("attachments") or []):
            name = str((attachment or {}).get("name") or "")
            ext = Path(name).suffix.lower()
            parsed = list((attachment or {}).get("parsed_content") or [])
            for row in parsed:
                if isinstance(row, dict):
                    raw_preview_chars += len(str(row.get("preview_content") or ""))

            if ext in FILE_EXTENSIONS:
                if parsed:
                    suppressed_file_previews += 1
                attachment["parsed_content"] = []
                continue

            if ext in IMAGE_EXTENSIONS:
                changed = False
                new_rows = []
                for row in parsed:
                    if not isinstance(row, dict):
                        continue
                    new_row = dict(row)
                    before = str(new_row.get("preview_content") or "")
                    after = trim_image_preview(before)
                    new_row["preview_content"] = after
                    if after != before.strip():
                        changed = True
                    if after:
                        kept_preview_chars += len(after)
                        new_rows.append(new_row)
                if changed:
                    trimmed_image_previews += 1
                attachment["parsed_content"] = new_rows
                continue

            for row in parsed:
                if isinstance(row, dict):
                    kept_preview_chars += len(str(row.get("preview_content") or ""))

    stats = {
        "model_message_count": len(messages),
        "raw_attachment_preview_chars_first48": raw_preview_chars,
        "kept_attachment_preview_chars_first48": kept_preview_chars,
        "removed_attachment_preview_chars_first48": raw_preview_chars - kept_preview_chars,
        "suppressed_file_previews": suppressed_file_previews,
        "trimmed_image_previews": trimmed_image_previews,
        "chat_original_text_modified": False,
    }
    return doc, stats


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: t47_shangtang_first48_attachment_light_low3000_retry429.py SOURCE_ROOT OUTPUT_DIR")
    root = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root / "l1" / "tools"))
    sys.path.insert(0, str(root / "l1"))

    import run_research_full_context_matter_baseline as baseline
    import build_research_full_context_human_packet_simple_thinking as human

    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")
    source_sha = os.environ["SOURCE_SHA"]
    if not api_key or not model:
        raise SystemExit("missing Shangtang configuration before provider call")

    original = baseline.build_full_context_input(
        shared_replay=root / "context/evaluation/company_shared_local_semantics_slice_a.jsonl",
        fixture_zip=root / "l1/tests/fixtures/obsolete_media_test_data.zip",
        l1_candidate_db=root / "l1/evaluation/vnext/runtime/full_context_matter_input/l1_candidate_state.db",
        l1_run_id="run_c46e10f6833adb71",
    )
    if (original.get("user_payload") or {}).get("mature_matters"):
        raise SystemExit("mature_matters must be empty")

    model_input, filter_stats = sanitize_first48(original)
    baseline._write_json(out / "full_context_input.json", model_input)
    packet, sidecar, stats = human.build_human_packet(model_input)
    stats = dict(stats)
    stats.update(filter_stats)

    if "消息49 |" in packet:
        raise SystemExit("first48 packet unexpectedly contains message49")
    if stats.get("message_count") != 48:
        raise SystemExit("first48 packet did not contain exactly 48 messages")

    (out / "human_context_packet.txt").write_text(packet, encoding="utf-8")
    (out / "human_context_system_prompt.txt").write_text(human.SYSTEM_PROMPT + "\n", encoding="utf-8")
    write_json(out / "human_context_map.json", sidecar)
    write_json(out / "human_context_stats.json", stats)

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

    for attempt in (1, 2):
        req = urllib.request.Request(
            url,
            data=request_bytes,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
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
            if int(exc.code) == 429 and attempt == 1:
                print("FIRST_ATTEMPT_HTTP_429_WAIT_60S_SINGLE_RETRY")
                time.sleep(RETRY_429_DELAY_SECONDS)
                continue
            write_json(out / "full_context_raw_result.json", {
                "schema_version": "research_first48_attachment_light_low3000_raw_result.v1",
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
        "schema_version": "research_first48_attachment_light_low3000_raw_result.v1",
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
    print("model_message_count=48")
    print("reasoning_effort=low")
    print("max_tokens=3000")
    print("request_bytes=" + str(len(request_bytes)))
    print("packet_chars=" + str(len(packet)))
    print("attachment_preview_chars=" + str(stats.get("attachment_preview_chars")))
    print("removed_attachment_preview_chars=" + str(stats.get("removed_attachment_preview_chars_first48")))
    print("prompt_tokens=" + str(usage.get("prompt_tokens") or usage.get("input_tokens") or ""))
    print("completion_tokens=" + str(usage.get("completion_tokens") or usage.get("output_tokens") or ""))
    print("reasoning_tokens=" + str(details.get("reasoning_tokens") if isinstance(details, dict) else ""))
    print("total_tokens=" + str(usage.get("total_tokens") or ""))
    print("finish_reason=" + str(finish_reason or ""))
    print("json_parse_ok=" + str(parsed is not None).lower())
    print("content_chars=" + str(len(content) if isinstance(content, str) else -1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
