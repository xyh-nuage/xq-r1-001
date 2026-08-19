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
MAX_TOKENS = 6000


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_response(envelope: dict) -> tuple[dict, str, str]:
    payload = envelope.get("data") if isinstance(envelope.get("data"), dict) else envelope
    if not isinstance(payload, dict):
        raise ValueError("provider response payload is not an object")
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise ValueError("provider response has no choice")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else message
    if not isinstance(content, str):
        raise ValueError("provider response content is not text")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else envelope.get("usage")
    model = str(payload.get("model") or envelope.get("model") or "")
    return dict(usage or {}), content, model


def remap_source_ids(value, source_map: dict[str, str]):
    if isinstance(value, list):
        return [remap_source_ids(v, source_map) for v in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        if key == "source_ids" and isinstance(item, list):
            out[key] = [source_map.get(str(v), str(v)) for v in item]
        else:
            out[key] = remap_source_ids(item, source_map)
    return out


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: t31_shangtang_compact_one_call.py SOURCE_ROOT OUTPUT_DIR")
    root = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root / "l1" / "tools"))
    sys.path.insert(0, str(root / "l1"))
    import run_research_full_context_matter_baseline as b
    import compact_research_full_context_model_input as c

    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")
    source_sha = os.environ["SOURCE_SHA"]
    if not api_key:
        raise SystemExit("LLM_API_KEY missing before provider call")
    if not model:
        raise SystemExit("LLM_MODEL missing before provider call")

    shared = root / "context/evaluation/company_shared_local_semantics_slice_a.jsonl"
    fixture = root / "l1/tests/fixtures/obsolete_media_test_data.zip"
    l1db = root / "l1/evaluation/vnext/runtime/full_context_matter_input/l1_candidate_state.db"
    silver1 = root / "context/evaluation/continuous_slice_a_matter_gold_redraft.v1.json"
    silver2 = root / "context/evaluation/continuous_slice_a_matter_gold_redraft.v2.json"

    original = b.build_full_context_input(
        shared_replay=shared, fixture_zip=fixture, l1_candidate_db=l1db,
        l1_run_id="run_c46e10f6833adb71",
    )
    original_path = out / "full_context_input.json"
    b._write_json(original_path, original)

    compact, sidecar = c.compact_model_input(original)
    compact["system_prompt"] = compact["system_prompt"] + (
        "\n\n输入消息的 id 已压缩为 S001、S002……。输出 JSON 中所有 source_ids 必须只使用这些 Sxxx id；"
        "不要尝试恢复或生成原始 stable id。"
    )
    compact_path = out / "full_context_model_input.json"
    map_path = out / "full_context_model_input_map.json"
    stats_path = out / "full_context_model_input_stats.json"
    write_json(compact_path, compact)
    write_json(map_path, sidecar)
    write_json(stats_path, c.measure(original, compact))

    request_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": compact["system_prompt"]},
            {"role": "user", "content": json.dumps(compact["user_payload"], ensure_ascii=False, separators=(",", ":"))},
        ],
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    request_bytes = json.dumps(request_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request_url = PROVIDER_BASE_URL.rstrip("/") + "/chat/completions"
    request = urllib.request.Request(
        request_url, data=request_bytes,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )

    started = time.monotonic()
    try:
        # USER AUTHORIZATION BOUNDARY: exactly one provider HTTP request; no retry/fallback.
        with urllib.request.urlopen(request, timeout=180) as response:
            response_headers = dict(response.headers.items())
            raw_bytes = response.read()
        envelope = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        if not isinstance(envelope, dict):
            raise ValueError("provider envelope is not an object")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")[:4000]
        headers = dict(exc.headers.items()) if exc.headers else {}
        failure = {
            "schema_version": "research_full_context_matter_compact_raw_result.v1",
            "status": "MODEL_CALL_FAILED_PRE_REFERENCE",
            "exact_source_commit": source_sha,
            "provider": urlparse(PROVIDER_BASE_URL).netloc,
            "provider_base_url": PROVIDER_BASE_URL,
            "configured_model": model,
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
            "request_bytes": len(request_bytes),
            "model_call_count": 1,
            "retry_count": 0,
            "company_content_sent_to_model": True,
            "request_id": headers.get("x-request-id") or headers.get("request-id"),
            "error": {"type": "HTTPError", "http_status": int(exc.code), "body": error_body},
            "silver_gold_reference_opened_before_this_file": False,
        }
        write_json(out / "full_context_raw_result.json", failure)
        print(f"ONE_CALL_STATUS=HTTP_{int(exc.code)}_NO_RETRY")
        return 20
    except Exception as exc:
        failure = {
            "schema_version": "research_full_context_matter_compact_raw_result.v1",
            "status": "MODEL_CALL_FAILED_PRE_REFERENCE",
            "exact_source_commit": source_sha,
            "provider": urlparse(PROVIDER_BASE_URL).netloc,
            "provider_base_url": PROVIDER_BASE_URL,
            "configured_model": model,
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
            "request_bytes": len(request_bytes),
            "model_call_count": 1,
            "retry_count": 0,
            "company_content_sent_to_model": True,
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "silver_gold_reference_opened_before_this_file": False,
        }
        write_json(out / "full_context_raw_result.json", failure)
        print("ONE_CALL_STATUS=MODEL_CALL_FAILED_NO_RETRY")
        return 20

    elapsed_ms = int((time.monotonic() - started) * 1000)
    try:
        usage, content, response_model = extract_response(envelope)
        parsed_compact = b._parse_json(content)
        source_map = dict(sidecar["source_id_map"])
        remapped = remap_source_ids(parsed_compact, source_map)
        valid_ids = set(source_map.values())
        normalized = b.normalize_model_output(remapped, valid_ids)
    except Exception as exc:
        failure = {
            "schema_version": "research_full_context_matter_compact_raw_result.v1",
            "status": "MODEL_RESPONSE_UNREADABLE_PRE_REFERENCE",
            "exact_source_commit": source_sha,
            "provider": urlparse(PROVIDER_BASE_URL).netloc,
            "configured_model": model,
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
            "request_bytes": len(request_bytes),
            "model_call_count": 1,
            "retry_count": 0,
            "company_content_sent_to_model": True,
            "elapsed_ms": elapsed_ms,
            "provider_envelope": envelope,
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "silver_gold_reference_opened_before_this_file": False,
        }
        write_json(out / "full_context_raw_result.json", failure)
        print("ONE_CALL_STATUS=UNREADABLE_RESPONSE_NO_RETRY")
        return 21

    raw = {
        "schema_version": "research_full_context_matter_compact_raw_result.v1",
        "status": "MODEL_RESULT_PERSISTED_PRE_REFERENCE",
        "exact_source_commit": source_sha,
        "provider": urlparse(PROVIDER_BASE_URL).netloc,
        "provider_base_url": PROVIDER_BASE_URL,
        "configured_model": model,
        "response_model_version": response_model or model,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "request_bytes": len(request_bytes),
        "model_call_count": 1,
        "retry_count": 0,
        "company_content_sent_to_model": True,
        "request_id": response_headers.get("x-request-id") or response_headers.get("request-id"),
        "elapsed_ms": elapsed_ms,
        "token_usage": usage,
        "raw_model_content": content,
        "parsed_compact_json": parsed_compact,
        "normalized_json": normalized,
        "silver_gold_reference_opened_before_this_file": False,
    }
    # Persist raw result before opening any reference labels.
    write_json(out / "full_context_raw_result.json", raw)

    evaluation = b.run_posthoc_phase(
        output_dir=out, normalized=normalized,
        silver_v1_path=silver1, silver_v2_path=silver2,
        all_source_ids=set(sidecar["source_id_map"].values()),
    )
    write_json(out / "flat.json", b._flat(normalized, raw, evaluation))
    print("ONE_CALL_STATUS=SUCCESS")
    print("model_calls=1")
    print("retry_count=0")
    print("request_bytes=" + str(len(request_bytes)))
    print("prompt_tokens=" + str(usage.get("prompt_tokens") or usage.get("input_tokens") or ""))
    print("completion_tokens=" + str(usage.get("completion_tokens") or usage.get("output_tokens") or ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
