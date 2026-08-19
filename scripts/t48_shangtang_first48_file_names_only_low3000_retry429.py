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

# For image previews, remove only obviously visual/camera/environmental prose.
# Do not rewrite chat text and do not summarize document files.
NOISE_TERMS = (
    "vivo", "AURA LIGHT", "ISO", "f/", "相机水印", "拍摄参数",
    "木纹桌面", "天花板", "混凝土地面", "金属瓦楞", "低角度",
    "亮红橙色", "红色涂装", "白蓝相间", "仓库内部", "背景中",
    "叉车", "梁柱", "支撑结构", "车轮",
)
STRONG_BUSINESS_TERMS = (
    "车牌", "车号", "货物", "木薯", "淀粉", "装载", "提单", "合同", "订单",
    "司机", "姓名", "电话", "身份证", "地址", "日期", "时间", "称重", "净重",
    "毛重", "重量", "吨", "kg", "KG", "签收", "签名", "收货", "发货", "托盘",
    "包", "柜", "批次", "编号", "品牌", "产品", "出库单",
)

KNOWN_FILE_CONTENT_MARKERS = (
    "6800元/辆",
    "发货须知:",
    "### Sheet: Sheet1",
    "本提货委托单编号是唯一",
)
REQUIRED_FILE_NAMES = (
    "青岛远腾-杭州钱塘.pdf",
    "杰坤-青岛-杭州报价确认 双签.pdf",
    "提货委托书_20260706_新创云联_34_太仓-杰坤_木薯淀粉.pdf",
    "2026年  新创云联 物流跟踪表.xlsx",
)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def is_file_message(text: str) -> bool:
    first = str(text or "").lstrip().splitlines()[0].strip() if str(text or "").strip() else ""
    return first == "文件"


def is_table_like(text: str) -> bool:
    text = str(text or "")
    return text.count("|") >= 6


def trim_image_preview(text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return ""

    # OCR/table-like content is business evidence; preserve it verbatim.
    if is_table_like(text):
        return text

    kept: list[str] = []
    for raw_line in text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        for part in re.split(r"(?<=[。！？；])", raw_line):
            part = part.strip()
            if not part:
                continue
            has_noise = any(term in part for term in NOISE_TERMS)
            has_business = any(term in part for term in STRONG_BUSINESS_TERMS)
            if has_noise and not has_business:
                continue
            kept.append(part)
    return "\n".join(kept).strip()


def sanitize_first48(input_doc: dict) -> tuple[dict, dict]:
    doc = copy.deepcopy(input_doc)
    payload = doc.get("user_payload") or {}
    messages = list(payload.get("messages") or [])[:48]
    payload["messages"] = messages
    doc["user_payload"] = payload

    raw_preview_chars = 0
    kept_preview_chars = 0
    suppressed_file_preview_chars = 0
    suppressed_file_messages = 0
    trimmed_image_previews = 0

    for message in messages:
        row = dict(message or {})
        original_text = str(row.get("original_text") or "")
        file_message = is_file_message(original_text)
        if file_message:
            suppressed_file_messages += 1

        for attachment in list((message or {}).get("attachments") or []):
            parsed = list((attachment or {}).get("parsed_content") or [])
            preview_chars_here = sum(
                len(str(item.get("preview_content") or ""))
                for item in parsed
                if isinstance(item, dict)
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
                after = trim_image_preview(before)
                if after != before:
                    changed = True
                if after:
                    new_item["preview_content"] = after
                    kept_preview_chars += len(after)
                    new_rows.append(new_item)
            if changed:
                trimmed_image_previews += 1
            attachment["parsed_content"] = new_rows

    stats = {
        "model_message_count": len(messages),
        "raw_attachment_preview_chars_first48": raw_preview_chars,
        "kept_attachment_preview_chars_first48": kept_preview_chars,
        "removed_attachment_preview_chars_first48": raw_preview_chars - kept_preview_chars,
        "suppressed_file_preview_chars_first48": suppressed_file_preview_chars,
        "suppressed_file_messages": suppressed_file_messages,
        "trimmed_image_previews": trimmed_image_previews,
        "chat_original_text_modified": False,
        "file_policy": "file message keeps original chat/file name; parsed attachment preview suppressed",
        "image_policy": "OCR/table preserved; obvious camera/environment prose removed",
    }
    return doc, stats


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: t48_shangtang_first48_file_names_only_low3000_retry429.py SOURCE_ROOT OUTPUT_DIR")

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
        l1_run_id="run_c46e10f683adb71",
    )
    if (original.get("user_payload") or {}).get("mature_matters"):
        raise SystemExit("mature_matters must be empty")

    model_input, filter_stats = sanitize_first48(original)
    baseline._write_json(out / "full_context_input.json", model_input)
    packet, sidecar, stats = human.build_human_packet(model_input)
    stats = dict(stats)
    stats.update(filter_stats)

    # Pre-provider isolation gates: if any of these fail, no provider call is made.
    if stats.get("message_count") != 48 or stats.get("model_message_count") != 48:
        raise SystemExit("first48 packet must contain exactly 48 messages")
    if stats.get("suppressed_file_messages", 0) < 5:
        raise SystemExit("expected at least five file messages; refuse provider call")
    if stats.get("suppressed_file_preview_chars_first48", 0) < 1500:
        raise SystemExit("file previews were not materially suppressed; refuse provider call")
    for marker in KNOWN_FILE_CONTENT_MARKERS:
        if marker in packet:
            raise SystemExit(f"file body marker still present in packet: {marker}")
    for file_name in REQUIRED_FILE_NAMES:
        if file_name not in packet:
            raise SystemExit(f"required file name missing from chat text: {file_name}")
    if "13608997076" not in packet or "372824196909060636" not in packet:
        raise SystemExit("chat phone/id evidence was unexpectedly modified")
    if "消息49 |" in packet:
        raise SystemExit("first48 packet unexpectedly contains message49")

    (out / "human_context_packet.txt").write_text(packet, encoding="utf-8")
    (out / "human_context_system_prompt.txt").write_text(human.SYSTEM_PROMPT + "\n", encoding="utf-8")
    write_json(out / "human_context_map.json", sidecar)
    write_json(out / "human_context_stats.json", stats)

    print("PACKET_GATE_PASS")
    print("packet_chars=" + str(len(packet)))
    print("attachment_preview_chars=" + str(stats.get("attachment_preview_chars")))
    print("raw_attachment_preview_chars_first48=" + str(stats.get("raw_attachment_preview_chars_first48")))
    print("kept_attachment_preview_chars_first48=" + str(stats.get("kept_attachment_preview_chars_first48")))
    print("suppressed_file_preview_chars_first48=" + str(stats.get("suppressed_file_preview_chars_first48")))
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
                "schema_version": "research_first48_file_names_only_low3000_raw_result.v1",
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
        "schema_version": "research_first48_file_names_only_low3000_raw_result.v1",
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
