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
MAX_TOKENS = 1800


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: t34_shangtang_human_packet_one_call.py SOURCE_ROOT OUTPUT_DIR")
    root = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root / "l1" / "tools"))
    sys.path.insert(0, str(root / "l1"))

    import run_research_full_context_matter_baseline as baseline
    import build_research_full_context_human_packet as human

    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")
    source_sha = os.environ["SOURCE_SHA"]
    if not api_key or not model:
        raise SystemExit("missing Shangtang model configuration before provider call")

    shared = root / "context/evaluation/company_shared_local_semantics_slice_a.jsonl"
    fixture = root / "l1/tests/fixtures/obsolete_media_test_data.zip"
    l1db = root / "l1/evaluation/vnext/runtime/full_context_matter_input/l1_candidate_state.db"

    original = baseline.build_full_context_input(
        shared_replay=shared,
        fixture_zip=fixture,
        l1_candidate_db=l1db,
        l1_run_id="run_c46e10f6833adb71",
    )
    baseline._write_json(out / "full_context_input.json", original)

    packet, sidecar, stats = human.build_human_packet(original)
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
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    request_bytes = json.dumps(request_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request_url = PROVIDER_BASE_URL.rstrip("/") + "/chat/completions"
    request = urllib.request.Request(
        request_url,
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
        # Authorization boundary: exactly one provider HTTP request. No retry or fallback.
        with urllib.request.urlopen(request, timeout=180) as response:
            status = int(response.status)
            headers = dict(response.headers.items())
            raw_bytes = response.read()
        envelope = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:4000]
        headers = dict(exc.headers.items()) if exc.headers else {}
        failure = {
            "schema_version": "research_full_context_human_packet_raw_result.v1",
            "status": "MODEL_CALL_FAILED_PRE_REFERENCE",
            "exact_source_commit": source_sha,
            "provider": urlparse(PROVIDER_BASE_URL).netloc,
            "provider_base_url": PROVIDER_BASE_URL,
            "configured_model": model,
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
            "request_bytes": len(request_bytes),
            "packet_stats": stats,
            "model_call_count": 1,
            "retry_count": 0,
            "company_content_sent_to_model": True,
            "request_id": headers.get("x-request-id") or headers.get("request-id"),
            "error": {"type": "HTTPError", "http_status": int(exc.code), "body": body},
            "silver_gold_reference_opened_before_this_file": False,
        }
        write_json(out / "full_context_raw_result.json", failure)
        print(f"ONE_CALL_STATUS=HTTP_{int(exc.code)}_NO_RETRY")
        print("request_bytes=" + str(len(request_bytes)))
        return 20
    except Exception as exc:
        failure = {
            "schema_version": "research_full_context_human_packet_raw_result.v1",
            "status": "MODEL_CALL_FAILED_PRE_REFERENCE",
            "exact_source_commit": source_sha,
            "provider": urlparse(PROVIDER_BASE_URL).netloc,
            "configured_model": model,
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
            "request_bytes": len(request_bytes),
            "packet_stats": stats,
            "model_call_count": 1,
            "retry_count": 0,
            "company_content_sent_to_model": True,
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "silver_gold_reference_opened_before_this_file": False,
        }
        write_json(out / "full_context_raw_result.json", failure)
        print("ONE_CALL_STATUS=TRANSPORT_OR_RESPONSE_ERROR_NO_RETRY")
        print("request_bytes=" + str(len(request_bytes)))
        return 21

    elapsed_ms = int((time.monotonic() - started) * 1000)
    payload = envelope.get("data") if isinstance(envelope, dict) and isinstance(envelope.get("data"), dict) else envelope
    usage = payload.get("usage") if isinstance(payload, dict) and isinstance(payload.get("usage"), dict) else {}
    choices = payload.get("choices") if isinstance(payload, dict) else []
    message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    content = message.get("content") if isinstance(message, dict) else None
    reasoning_content = message.get("reasoning_content") if isinstance(message, dict) else None

    raw = {
        "schema_version": "research_full_context_human_packet_raw_result.v1",
        "status": "MODEL_HTTP_SUCCESS_PRE_REFERENCE",
        "exact_source_commit": source_sha,
        "provider": urlparse(PROVIDER_BASE_URL).netloc,
        "provider_base_url": PROVIDER_BASE_URL,
        "configured_model": model,
        "response_model_version": str(payload.get("model") or model) if isinstance(payload, dict) else model,
        "http_status": status,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "request_bytes": len(request_bytes),
        "packet_stats": stats,
        "model_call_count": 1,
        "retry_count": 0,
        "company_content_sent_to_model": True,
        "request_id": headers.get("x-request-id") or headers.get("request-id"),
        "elapsed_ms": elapsed_ms,
        "token_usage": usage,
        "raw_model_content": content,
        "raw_reasoning_content": reasoning_content,
        "provider_envelope": envelope,
        "silver_gold_reference_opened_before_this_file": False,
    }
    write_json(out / "full_context_raw_result.json", raw)
    print("ONE_CALL_STATUS=HTTP_200_NO_RATE_LIMIT")
    print("model_calls=1")
    print("retry_count=0")
    print("request_bytes=" + str(len(request_bytes)))
    print("packet_chars=" + str(stats.get("packet_chars", "")))
    print("prompt_tokens=" + str(usage.get("prompt_tokens") or usage.get("input_tokens") or ""))
    print("completion_tokens=" + str(usage.get("completion_tokens") or usage.get("output_tokens") or ""))
    print("total_tokens=" + str(usage.get("total_tokens") or ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
