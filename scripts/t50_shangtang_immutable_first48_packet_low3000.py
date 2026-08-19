#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

PROVIDER_BASE_URL = "https://token.sensenova.cn/v1/"
MAX_TOKENS = 3000
REASONING_EFFORT = "low"
RETRY_429_DELAY_SECONDS = 60


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("usage: t50_shangtang_immutable_first48_packet_low3000.py PACKET PROMPT OUTPUT_DIR")
    packet_path = Path(sys.argv[1])
    prompt_path = Path(sys.argv[2])
    out = Path(sys.argv[3])
    out.mkdir(parents=True, exist_ok=True)

    packet = packet_path.read_text(encoding="utf-8")
    system_prompt = prompt_path.read_text(encoding="utf-8").rstrip("\n")
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")
    source_sha = os.environ.get("SOURCE_SHA", "")
    packet_source_commit = os.environ.get("PACKET_SOURCE_COMMIT", "")
    if not api_key or not model or not source_sha or not packet_source_commit:
        raise SystemExit("missing required environment")

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
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
                "schema_version": "research_immutable_first48_low3000_raw_result.v1",
                "status": "MODEL_CALL_FAILED_PRE_REFERENCE",
                "exact_source_commit": source_sha,
                "packet_source_commit": packet_source_commit,
                "provider": urlparse(PROVIDER_BASE_URL).netloc,
                "configured_model": model,
                "thinking": {"type": "enabled"},
                "reasoning_effort": REASONING_EFFORT,
                "max_tokens": MAX_TOKENS,
                "packet_chars": len(packet),
                "system_prompt_chars": len(system_prompt),
                "request_bytes": len(request_bytes),
                "model_call_count": len(attempts),
                "retry_count": max(0, len(attempts)-1),
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
        "schema_version": "research_immutable_first48_low3000_raw_result.v1",
        "status": "MODEL_HTTP_SUCCESS_PRE_REFERENCE",
        "exact_source_commit": source_sha,
        "packet_source_commit": packet_source_commit,
        "provider": urlparse(PROVIDER_BASE_URL).netloc,
        "configured_model": model,
        "response_model_version": str(payload.get("model") or model) if isinstance(payload, dict) else model,
        "http_status": status,
        "thinking": {"type": "enabled"},
        "reasoning_effort": REASONING_EFFORT,
        "max_tokens": MAX_TOKENS,
        "packet_chars": len(packet),
        "system_prompt_chars": len(system_prompt),
        "request_bytes": len(request_bytes),
        "model_call_count": len(attempts),
        "retry_count": max(0, len(attempts)-1),
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
    print("retry_count=" + str(max(0, len(attempts)-1)))
    print("packet_chars=" + str(len(packet)))
    print("system_prompt_chars=" + str(len(system_prompt)))
    print("request_bytes=" + str(len(request_bytes)))
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
