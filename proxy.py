"""
proxy.py
--------
The actual OpenAI-compatible API surface. Point any AI tool (OpenHands,
Continue, Cursor, raw OpenAI SDK, etc.) at http://<host>:8000/v1

Upstream backend: llmproxy.org/api/chat.php — already speaks a near-OpenAI
SSE format, so this version streams chunks through live (transformed one
at a time via rules_active.py) rather than collecting everything first
and emitting once, which the previous (different) upstream required.

Every request/response is logged to store.py (in-memory) and visible live
in the dashboard (dashboard.py, port 8001).

Transformation logic (model name mapping, request shaping, per-chunk
response cleanup) lives in rules_active.py and is reloaded fresh on every
request, so edits made from the dashboard's "Proxy Rules" editor apply
immediately.
"""

import json
import time
import uuid
import logging
import traceback

from flask import Flask, request, Response, stream_with_context
import requests

import store
import rules

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

UPSTREAM_URL = "https://llmproxy.org/api/chat.php"

UPSTREAM_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://freegpt.chat",
    "Referer": "https://freegpt.chat/",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("llm-proxy")

app = Flask(__name__)


# ---------------------------------------------------------------------------
# OpenAI-shaped errors
# ---------------------------------------------------------------------------

def openai_error(message, err_type="invalid_request_error", status=400, code=None):
    body = {"error": {"message": message, "type": err_type, "param": None, "code": code}}
    log.error("Returning error to client: %s", message)
    return Response(json.dumps(body, ensure_ascii=False), status=status, mimetype="application/json; charset=utf-8")


# ---------------------------------------------------------------------------
# SSE line parsing helpers
# ---------------------------------------------------------------------------

def iter_upstream_sse(upstream_payload):
    """
    Generator over raw upstream SSE lines, already split into:
      - "data" events with parsed JSON (yields ("data", dict))
      - the literal [DONE] marker                (yields ("done", None))
      - anything else (comments like ": OPENROUTER PROCESSING", blank
        lines, or unparsable data) is silently skipped — these aren't
        real content and every captured example shows they carry nothing
        the caller needs.

    IMPORTANT: we read raw bytes (decode_unicode=False, the default) and
    decode them ourselves as UTF-8 explicitly. requests' decode_unicode=True
    path uses the response's apparent/guessed encoding, which can silently
    fall back to ISO-8859-1/Latin-1 on SSE streams whose Content-Type
    doesn't declare "charset=utf-8" explicitly. That wrong guess corrupts
    every multi-byte UTF-8 character it touches - Arabic, Cyrillic,
    Chinese, emoji, accented Latin, etc. all get mangled into nonsense
    (e.g. Arabic "أ" turning into the two garbage characters "Ø£"). Forcing
    UTF-8 decoding here guarantees correct handling of any language the
    upstream model responds in, regardless of what encoding the HTTP
    response headers claim or omit.

    Raises requests.exceptions.RequestException on network failure.
    """
    with requests.post(UPSTREAM_URL, headers=UPSTREAM_HEADERS, json=upstream_payload,
                        stream=True, timeout=180) as r:
        r.raise_for_status()
        for raw_bytes_line in r.iter_lines(decode_unicode=False):
            if not raw_bytes_line:
                continue
            try:
                raw_line = raw_bytes_line.decode("utf-8")
            except UnicodeDecodeError as e:
                log.warning("Skipping line that failed UTF-8 decode: %s", e)
                continue
            if raw_line.startswith(":"):
                # SSE comment line (e.g. ": OPENROUTER PROCESSING") - ignore
                continue
            if not raw_line.startswith("data:"):
                continue
            data_str = raw_line[len("data:"):].strip()
            if data_str == "[DONE]":
                yield ("done", None)
                return
            try:
                obj = json.loads(data_str)
            except json.JSONDecodeError:
                log.debug("Skipping unparsable SSE data line: %r", data_str[:200])
                continue
            yield ("data", obj)


# ---------------------------------------------------------------------------
# OpenAI-shaped non-streaming response builder (for stream=false requests)
# ---------------------------------------------------------------------------

def to_openai_completion(full_text, model, chunk_id, created, finish_reason="stop", usage=None):
    return {
        "id": chunk_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": full_text},
            "finish_reason": finish_reason,
        }],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    start_time = time.time()
    raw_body_text = request.get_data(as_text=True)

    try:
        body = request.get_json(force=True, silent=False)
    except Exception as e:
        return openai_error(f"Invalid JSON body: {e}")

    if not body:
        return openai_error("Request body is empty or not valid JSON")

    messages = body.get("messages")
    requested_model = body.get("model", "gpt-4o")
    stream = bool(body.get("stream", False))

    if not messages or not isinstance(messages, list):
        return openai_error("'messages' is required and must be a non-empty array")

    num_files_estimate = sum(
        1 for m in messages
        if isinstance(m.get("content"), list)
        for part in m["content"]
        if isinstance(part, dict) and part.get("type") in ("file", "image_url")
    )

    entry_id = store.new_entry(
        method="POST",
        path="/v1/chat/completions",
        raw_request_body=raw_body_text,
        model=requested_model,
        stream=stream,
        num_messages=len(messages),
        num_files=num_files_estimate,
    )

    # Load live rules fresh - this is what makes dashboard edits to
    # rules_active.py apply without restarting the proxy.
    rules_mod = rules.load_rules()
    if rules_mod is None:
        err = rules.get_last_error()
        store.update_entry(entry_id, status="error", error=f"rules_active.py failed to load: {err}",
                            duration_ms=int((time.time() - start_time) * 1000))
        return openai_error(f"Proxy rules file has an error: {err}", err_type="internal_error", status=500)

    try:
        upstream_payload = rules_mod.build_upstream_request(messages, requested_model, stream)
    except Exception as e:
        err = f"build_upstream_request raised: {e}\n{traceback.format_exc()}"
        log.error(err)
        store.update_entry(entry_id, status="error", error=err,
                            duration_ms=int((time.time() - start_time) * 1000))
        return openai_error(f"Failed to process messages/files: {e}", status=500, err_type="internal_error")

    store.update_entry(entry_id, chat_text_sent=json.dumps(upstream_payload, ensure_ascii=False), context_sent=None)
    resolved_model = upstream_payload.get("model", requested_model)
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    if stream:
        def generate():
            collected_text_parts = []
            try:
                upstream_objects_log = []
                for kind, payload in iter_upstream_sse(upstream_payload):
                    if kind == "done":
                        break
                    upstream_objects_log.append(payload)
                    try:
                        cleaned = rules_mod.transform_upstream_line(payload)
                    except Exception as e:
                        raise RuntimeError(f"transform_upstream_line raised: {e}") from e
                    if cleaned is None:
                        continue
                    for choice in cleaned.get("choices", []):
                        content = choice.get("delta", {}).get("content")
                        if content:
                            collected_text_parts.append(content)
                    yield f"data: {json.dumps(cleaned, ensure_ascii=False)}\n\n"

                store.update_entry(
                    entry_id, status="success", upstream_raw_lines=upstream_objects_log,
                    final_response_text="".join(collected_text_parts),
                    duration_ms=int((time.time() - start_time) * 1000),
                )
                yield "data: [DONE]\n\n"

            except requests.exceptions.Timeout:
                err = "Upstream LLM timed out"
                store.update_entry(entry_id, status="error", error=err,
                                    duration_ms=int((time.time() - start_time) * 1000))
                yield f"data: {json.dumps({'error': {'message': err, 'type': 'timeout_error'}})}\n\n"
                yield "data: [DONE]\n\n"
            except requests.exceptions.RequestException as e:
                err = f"Upstream request failed: {e}"
                store.update_entry(entry_id, status="error", error=err,
                                    duration_ms=int((time.time() - start_time) * 1000))
                yield f"data: {json.dumps({'error': {'message': err, 'type': 'upstream_error'}})}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                err = f"Internal error: {e}\n{traceback.format_exc()}"
                log.error(err)
                store.update_entry(entry_id, status="error", error=err,
                                    duration_ms=int((time.time() - start_time) * 1000))
                yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'internal_error'}})}\n\n"
                yield "data: [DONE]\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream; charset=utf-8",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Non-streaming: upstream itself only seems to support SSE per every
    # captured example (stream:true in every request shown), so even when
    # the CALLER asks for stream=false, we still call upstream with its
    # native streaming shape and just collect everything before responding
    # once.
    try:
        collected_text_parts = []
        last_usage = None
        last_finish_reason = "stop"
        upstream_objects_log = []
        for kind, payload in iter_upstream_sse(upstream_payload):
            if kind == "done":
                break
            upstream_objects_log.append(payload)
            cleaned = rules_mod.transform_upstream_line(payload)
            if cleaned is None:
                continue
            for choice in cleaned.get("choices", []):
                content = choice.get("delta", {}).get("content")
                if content:
                    collected_text_parts.append(content)
                if choice.get("finish_reason"):
                    last_finish_reason = choice["finish_reason"]
            if "usage" in cleaned:
                last_usage = cleaned["usage"]

        full_text = "".join(collected_text_parts)
        store.update_entry(entry_id, status="success", upstream_raw_lines=upstream_objects_log,
                            final_response_text=full_text,
                            duration_ms=int((time.time() - start_time) * 1000))

    except requests.exceptions.Timeout:
        store.update_entry(entry_id, status="error", error="Upstream LLM timed out",
                            duration_ms=int((time.time() - start_time) * 1000))
        return openai_error("Upstream LLM timed out", err_type="timeout_error", status=504)
    except requests.exceptions.RequestException as e:
        err = f"Upstream request failed: {e}"
        store.update_entry(entry_id, status="error", error=err,
                            duration_ms=int((time.time() - start_time) * 1000))
        return openai_error(err, err_type="upstream_error", status=502)
    except Exception as e:
        err = f"Internal error: {e}\n{traceback.format_exc()}"
        log.error(err)
        store.update_entry(entry_id, status="error", error=err,
                            duration_ms=int((time.time() - start_time) * 1000))
        return openai_error(str(e), err_type="internal_error", status=500)

    return Response(
        json.dumps(to_openai_completion(full_text, resolved_model, chunk_id, created,
                                         finish_reason=last_finish_reason, usage=last_usage),
                    ensure_ascii=False),
        mimetype="application/json; charset=utf-8",
    )


@app.route("/v1/models", methods=["GET"])
def models():
    return {"object": "list", "data": [
        {"id": "gpt-4o", "object": "model", "owned_by": "openai", "created": int(time.time())},
        {"id": "deepseek-v4-flash-20260423", "object": "model", "owned_by": "deepseek", "created": int(time.time())},
    ]}


@app.route("/v1/models/<model_id>", methods=["GET"])
def model_detail(model_id):
    return {"id": model_id, "object": "model", "owned_by": "local", "created": int(time.time())}


@app.route("/internal/log", methods=["GET"])
def internal_log():
    """Internal endpoint the dashboard process polls over HTTP (separate process, no shared memory)."""
    return {"entries": store.get_all(limit=200), "stats": store.stats()}


@app.route("/internal/log/clear", methods=["POST"])
def internal_log_clear():
    store.clear()
    return {"ok": True}


@app.errorhandler(404)
def not_found(e):
    return openai_error(f"Unknown route: {request.path}", err_type="not_found_error", status=404)


@app.errorhandler(500)
def internal_error(e):
    log.error("Unhandled exception: %s\n%s", e, traceback.format_exc())
    return openai_error("Internal proxy error", err_type="internal_error", status=500)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
