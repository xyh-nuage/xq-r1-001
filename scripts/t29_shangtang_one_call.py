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


def _extract_response(envelope: dict) -> tuple[dict, str]:
    payload = envelope.get("data") if isinstance(envelope.get("data"), dict) else envelope
    if not isinstance(payload, dict):
        raise ValueError("provider response payload is not an object")
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise ValueError("provider response has no choice")
    message = choices[0].get("message")
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = message
    if not isinstance(content, str):
        raise ValueError("provider response content is not text")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else envelope.get("usage")
    return dict(usage or {}), content


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: t29_shangtang_one_call.py SOURCE_ROOT OUTPUT_DIR")

    root = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root / "l1" / "tools"))
    sys.path.insert(0, str(root / "l1"))
    import run_research_full_context_matter_baseline as b

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

    input_document = b.build_full_context_input(
        shared_replay=shared,
        fixture_zip=fixture,
        l1_candidate_db=l1db,
        l1_run_id="run_c46e10f6833adb71",
    )
    input_path = out / "full_context_input.json"
    b._write_json(input_path, input_document)

    request_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": input_document["system_prompt"]},
            {
                "role": "user",
                "content": json.dumps(
                    input_document["user_payload"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    request_url = PROVIDER_BASE_URL.rstrip("/") + "/chat/completions"
    request = urllib.request.Request(
        request_url,
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    started = time.monotonic()
    request_id = None
    # USER AUTHORIZATION BOUNDARY: exactly one real provider HTTP request.
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
            raw_bytes = response.read()
        envelope = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        if not isinstance(envelope, dict):
            raise ValueError("provider envelope is not an object")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")[:4000]
        failure = {
            "schema_version": b.RESULT_SCHEMA,
            "task_id": b.TASK_ID,
            "status": "MODEL_CALL_FAILED_PRE_REFERENCE",
            "exact_source_commit": source_sha,
            "exact_model_input": b._file_identity(input_path),
            "provider": urlparse(PROVIDER_BASE_URL).netloc,
            "provider_base_url": PROVIDER_BASE_URL,
            "configured_model": model,
            "temperature": 0,
            "model_call_count": 1,
            "retry_count": 0,
            "company_content_sent_to_model": True,
            "error": {"type": "HTTPError", "http_status": int(exc.code), "body": error_body},
            "silver_gold_reference_opened_before_this_file": False,
        }
        b._write_json(out / "full_context_raw_result.json", failure)
        print(f"ONE_CALL_STATUS=HTTP_{int(exc.code)}_NO_RETRY")
        return 20
    except Exception as exc:
        failure = {
            "schema_version": b.RESULT_SCHEMA,
            "task_id": b.TASK_ID,
            "status": "MODEL_CALL_FAILED_PRE_REFERENCE",
            "exact_source_commit": source_sha,
            "exact_model_input": b._file_identity(input_path),
            "provider": urlparse(PROVIDER_BASE_URL).netloc,
            "provider_base_url": PROVIDER_BASE_URL,
            "configured_model": model,
            "temperature": 0,
            "model_call_count": 1,
            "retry_count": 0,
            "company_content_sent_to_model": True,
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "silver_gold_reference_opened_before_this_file": False,
        }
        b._write_json(out / "full_context_raw_result.json", failure)
        print("ONE_CALL_STATUS=MODEL_CALL_FAILED_NO_RETRY")
        return 20

    elapsed_ms = int((time.monotonic() - started) * 1000)
    attempt = {
        "attempt": 1,
        "request_id": request_id,
        "elapsed_ms": elapsed_ms,
        "request_url": request_url,
        "provider_envelope": envelope,
    }

    try:
        usage, content = _extract_response(envelope)
    except Exception as exc:
        failure = {
            "schema_version": b.RESULT_SCHEMA,
            "task_id": b.TASK_ID,
            "status": "MODEL_RESPONSE_UNREADABLE_PRE_REFERENCE",
            "exact_source_commit": source_sha,
            "exact_model_input": b._file_identity(input_path),
            "provider": urlparse(PROVIDER_BASE_URL).netloc,
            "provider_base_url": PROVIDER_BASE_URL,
            "configured_model": model,
            "temperature": 0,
            "model_call_count": 1,
            "retry_count": 0,
            "company_content_sent_to_model": True,
            "raw_provider_attempts": [attempt],
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "silver_gold_reference_opened_before_this_file": False,
        }
        b._write_json(out / "full_context_raw_result.json", failure)
        print("ONE_CALL_STATUS=UNREADABLE_RESPONSE_NO_RETRY")
        return 22

    attempt["raw_model_content"] = content
    try:
        parsed = b._parse_json(content)
    except Exception as exc:
        failure = {
            "schema_version": b.RESULT_SCHEMA,
            "task_id": b.TASK_ID,
            "status": "MODEL_RESULT_INVALID_JSON_PRE_REFERENCE",
            "exact_source_commit": source_sha,
            "exact_model_input": b._file_identity(input_path),
            "provider": urlparse(PROVIDER_BASE_URL).netloc,
            "provider_base_url": PROVIDER_BASE_URL,
            "configured_model": model,
            "response_model_version": str(envelope.get("model") or (envelope.get("data") or {}).get("model") or model),
            "temperature": 0,
            "model_call_count": 1,
            "retry_count": 0,
            "company_content_sent_to_model": True,
            "raw_provider_attempts": [attempt],
            "json_error": {"type": type(exc).__name__, "message": str(exc)},
            "normalized_json": None,
            "silver_gold_reference_opened_before_this_file": False,
        }
        b._write_json(out / "full_context_raw_result.json", failure)
        print("ONE_CALL_STATUS=INVALID_JSON_NO_RETRY")
        return 21

    valid_ids = {str(row["stable_id"]) for row in input_document["user_payload"]["messages"]}
    normalized = b.normalize_model_output(parsed, valid_ids)
    raw = {
        "schema_version": b.RESULT_SCHEMA,
        "task_id": b.TASK_ID,
        "status": "MODEL_RESULT_PERSISTED_PRE_REFERENCE",
        "exact_source_commit": source_sha,
        "exact_model_input": b._file_identity(input_path),
        "provider": urlparse(PROVIDER_BASE_URL).netloc,
        "provider_base_url": PROVIDER_BASE_URL,
        "configured_model": model,
        "response_model_version": str(envelope.get("model") or (envelope.get("data") or {}).get("model") or model),
        "system_prompt": input_document["system_prompt"],
        "temperature": 0,
        "token_usage": {
            "prompt_tokens": b._usage_int(usage, "prompt_tokens", "input_tokens"),
            "completion_tokens": b._usage_int(usage, "completion_tokens", "output_tokens"),
            "total_tokens": b._usage_int(usage, "total_tokens") or (
                b._usage_int(usage, "prompt_tokens", "input_tokens")
                + b._usage_int(usage, "completion_tokens", "output_tokens")
            ),
            "provider_usage": usage,
        },
        "cost": b._cost(usage, None, None),
        "model_call_count": 1,
        "json_parse_retry_count": 0,
        "retry_policy": "strictly no retry: user authorized exactly one model call",
        "company_content_sent_to_model": True,
        "raw_provider_attempts": [attempt],
        "normalized_json": normalized,
        "silver_gold_reference_opened_before_this_file": False,
    }
    b._write_json(out / "full_context_raw_result.json", raw)

    evaluation = b.run_posthoc_phase(
        output_dir=out,
        normalized=normalized,
        silver_v1_path=silver1,
        silver_v2_path=silver2,
        all_source_ids=valid_ids,
    )
    b._write_json(out / "flat.json", b._flat(normalized, raw, evaluation))

    print("ONE_CALL_STATUS=SUCCESS")
    print("model_calls=1")
    print("json_retries=0")
    print("predicted_matters=" + str(len(normalized.get("matters") or [])))
    print(
        "predicted_unresolved_sources="
        + str(len({x for r in normalized.get("unresolved") or [] for x in r.get("source_ids") or []}))
    )
    print("major_real_matters_found=" + str(evaluation["major_real_matters"]["found_count"]))
    print("major_real_matters_total=" + str(evaluation["major_real_matters"]["reference_concrete_matter_count"]))
    print(
        "false_merge_matters="
        + str(evaluation["false_merge"]["predicted_matters_merging_multiple_certain_reference_matters"])
    )
    print(
        "severe_split_reference_matters="
        + str(evaluation["severe_split"]["reference_matters_severely_split"])
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
