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


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: t41_shangtang_simple_thinking_max3000_one_call.py SOURCE_ROOT OUTPUT_DIR")
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
        raise SystemExit("missing Shangtang model configuration before provider call")

    original = baseline.build_full_context_input(
        shared_replay=root / "context/evaluation/company_shared_local_semantics_slice_a.jsonl",
        fixture_zip=root / "l1/tests/fixtures/obsolete_media_test_data.zip",
        l1_candidate_db=root / "l1/evaluation/vnext/runtime/full_context_matter_input/l1_candidate_state.db",
        l1_run_id="run_c46e10f6833adb71",
    )
    if (original.get("user_payload") or {}).get("mature_matters"):
        raise SystemExit("mature_matters must be empty for this baseline")
    baseline._write_json(out / "full_context_input.json", original)

    packet, sidecar, stats = human.build_human_packet(original)
    if stats.get("mature_matter_count") not in (None, 0):
        raise SystemExit("mature Matter leakage detected before provider call")
    (out / "human_context_packet.txt").write_text(packet, encoding="utf-8")
    (out / "human_context_system_prompt.txt").write_text(human.SYSTEM_PROMPT + "\n", encoding="utf-8")
    write_json(out / "human_context_map.json", sidecar)
    write_json(out / "human_context_stats.json", stats)

    request_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": human.SYSTEM_PROMPT},
            {"role": "user", "content": packet},
        ],
        "thinking": {"type": "enabled"},
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    request_bytes = json.dumps(request_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        PROVIDER_BASE_URL.rstrip("/") + "/chat/completions",
        data=request_bytes,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    started = time.monotonic()
    try:
        # USER AUTHORIZATION BOUNDARY: exactly one provider HTTP request; no retry/fallback.
        with urllib.request.urlopen(request, timeout=300) as response:
            status = int(response.status)
            headers = dict(response.headers.items())
            raw_bytes = response.read()
        envelope = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:4000]
        headers = dict(exc.headers.items()) if exc.headers else {}
        raw = {
            "schema_version": "research_full_context_simple_thinking_max3000_raw_result.v1",
            "status": "MODEL_CALL_FAILED_PRE_REFERENCE",
            "exact_source_commit": source_sha,
            "provider": urlparse(PROVIDER_BASE_URL).netloc,
            "configured_model": model,
            "thinking": {"type": "enabled"},
            "max_tokens": MAX_TOKENS,
            "request_bytes": len(request_bytes),
            "packet_stats": stats,
            "model_call_count": 1,
            "retry_count": 0,
            "error": {"type": "HTTPError", "http_status": int(exc.code), "body": body},
            "request_id": headers.get("x-request-id") or headers.get("request-id"),
            "silver_gold_reference_opened_before_this_file": False,
        }
        write_json(out / "full_context_raw_result.json", raw)
        print(f"ONE_CALL_STATUS=HTTP_{int(exc.code)}_NO_RETRY")
        print("max_tokens=3000")
        return 20
    except Exception as exc:
        raw = {
            "schema_version": "research_full_context_simple_thinking_max3000_raw_result.v1",
            "status": "MODEL_CALL_FAILED_PRE_REFERENCE",
            "exact_source_commit": source_sha,
            "provider": urlparse(PROVIDER_BASE_URL).netloc,
            "configured_model": model,
            "thinking": {"type": "enabled"},
            "max_tokens": MAX_TOKENS,
            "request_bytes": len(request_bytes),
            "packet_stats": stats,
            "model_call_count": 1,
            "retry_count": 0,
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "silver_gold_reference_opened_before_this_file": False,
        }
        write_json(out / "full_context_raw_result.json", raw)
        print("ONE_CALL_STATUS=TRANSPORT_OR_RESPONSE_ERROR_NO_RETRY")
        print("max_tokens=3000")
        return 21

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
        "schema_version": "research_full_context_simple_thinking_max3000_raw_result.v1",
        "status": "MODEL_HTTP_SUCCESS_PRE_REFERENCE",
        "exact_source_commit": source_sha,
        "provider": urlparse(PROVIDER_BASE_URL).netloc,
        "provider_base_url": PROVIDER_BASE_URL,
        "configured_model": model,
        "response_model_version": str(payload.get("model") or model) if isinstance(payload, dict) else model,
        "http_status": status,
        "temperature": 0,
        "thinking": {"type": "enabled"},
        "max_tokens": MAX_TOKENS,
        "request_bytes": len(request_bytes),
        "packet_stats": stats,
        "model_call_count": 1,
        "retry_count": 0,
        "company_content_sent_to_model": True,
        "request_id": headers.get("x-request-id") or headers.get("request-id"),
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
    print("ONE_CALL_STATUS=HTTP_200_NO_RATE_LIMIT")
    print("model_calls=1")
    print("retry_count=0")
    print("thinking_enabled=true")
    print("max_tokens=3000")
    print("request_bytes=" + str(len(request_bytes)))
    print("packet_chars=" + str(stats.get("packet_chars", "")))
    print("system_prompt_chars=" + str(len(human.SYSTEM_PROMPT)))
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
