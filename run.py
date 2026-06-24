"""
run.py
------
Single entry point for the Aven LLM proxy.

Running `python run.py` starts ONE Flask app on port 8000 that exposes:
  - The OpenAI-compatible proxy from proxy.py        (/v1/...)
  - User auth endpoint                                (/auth)
  - Usage stats endpoint                              (/usage)
  - Admin panel (login + user CRUD)                   (/admin/...)

Token counting + subscription enforcement happen on every /v1/chat/completions
request via a before_request hook that wraps the existing proxy endpoint.
"""

import csv
import logging
import os
import threading
import time
from datetime import datetime, date, timedelta

from flask import Flask, request, jsonify, Response, g

# Import the existing proxy (creates its own Flask `app` + routes)
import proxy as _proxy_mod
import store
import rules
from admin import admin_bp, USERS_CSV, _read_users, _write_users, _CSV_LOCK, _user_public

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("llm-proxy")

app = _proxy_mod.app
app.secret_key = os.environ.get("AVEN_SECRET_KEY", "aven-proxy-secret-2026")
app.register_blueprint(admin_bp)


# ---------- Token counting ----------

def _estimate_tokens(text):
    """Simple token estimate: words * 1.3 (clamped to >= 1 for non-empty text)."""
    if not text:
        return 0
    words = len(str(text).split())
    return max(1, int(words * 1.3))


def _find_user_by_code(code):
    """Read users.csv, return the matching user dict (or None)."""
    if not code:
        return None
    with _CSV_LOCK:
        users = _read_users()
    for u in users:
        if u.get("code") == code:
            return u
    return None


def _add_tokens_to_user(code, tokens):
    """Add `tokens` to the user's tokens_used in users.csv. Returns the updated user dict."""
    with _CSV_LOCK:
        users = _read_users()
        target = None
        for u in users:
            if u.get("code") == code:
                target = u
                break
        if not target:
            return None
        target["tokens_used"] = int(target.get("tokens_used", 0) or 0) + int(tokens)
        # Auto-deactivate if over limit
        if int(target["tokens_used"]) >= int(target.get("tokens_limit", 0) or 0):
            target["is_subscribed"] = 0
        _write_users(users)
        return target


def _deactivate_user(code, reason):
    """Set is_subscribed = 0 in users.csv (used when subscription expired)."""
    with _CSV_LOCK:
        users = _read_users()
        for u in users:
            if u.get("code") == code:
                u["is_subscribed"] = 0
                _write_users(users)
                return u
    return None


def _extract_code_from_request():
    """Pull the user's code from the Authorization header (Bearer <code>).

    Also accepts X-Aven-License header (sent by the Aven extension).
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    xaven = request.headers.get("X-Aven-License", "")
    if xaven:
        return xaven.strip()
    return ""


def _limit_reached_response(message):
    """OpenAI-shaped response for limit/expiry errors (non-streaming)."""
    return jsonify({
        "id": "chatcmpl-limit",
        "object": "chat.completion",
        "created": int(datetime.now().timestamp()),
        "model": "aven-limit",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": message},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })


# ---------- Auth endpoint with brute-force protection ----------

# Rate limiting: track failed auth attempts per IP.
# After MAX_FAILED_ATTEMPTS failed attempts, the IP is blocked for BLOCK_DURATION seconds.
# This makes brute-forcing the 20-char code practically impossible.
_AUTH_ATTEMPTS = {}  # ip → {"fails": int, "first_fail": timestamp, "blocked_until": timestamp}
_AUTH_LOCK = threading.Lock()
MAX_FAILED_ATTEMPTS = 5
BLOCK_DURATION = 300  # 5 minutes
_WINDOW = 600  # reset fail counter after 10 minutes of no attempts


def _check_rate_limit(ip):
    """Return (allowed, retry_after_seconds). If blocked, retry_after > 0."""
    now = time.time()
    with _AUTH_LOCK:
        rec = _AUTH_ATTEMPTS.get(ip)
        if not rec:
            return True, 0
        # If blocked and the block hasn't expired → deny
        if rec.get("blocked_until", 0) > now:
            return False, int(rec["blocked_until"] - now)
        # If the window has passed, reset
        if now - rec.get("first_fail", now) > _WINDOW:
            del _AUTH_ATTEMPTS[ip]
            return True, 0
        return True, 0


def _record_failed_attempt(ip):
    """Record a failed auth attempt; auto-blocks after MAX_FAILED_ATTEMPTS."""
    now = time.time()
    with _AUTH_LOCK:
        rec = _AUTH_ATTEMPTS.get(ip)
        if not rec or now - rec.get("first_fail", now) > _WINDOW:
            _AUTH_ATTEMPTS[ip] = {"fails": 1, "first_fail": now, "blocked_until": 0}
        else:
            rec["fails"] += 1
            if rec["fails"] >= MAX_FAILED_ATTEMPTS:
                rec["blocked_until"] = now + BLOCK_DURATION
                log.warning("[aven] IP %s blocked for %ds after %d failed auth attempts",
                            ip, BLOCK_DURATION, rec["fails"])


def _record_success(ip):
    """Clear the fail counter on a successful auth."""
    with _AUTH_LOCK:
        _AUTH_ATTEMPTS.pop(ip, None)


@app.route("/auth", methods=["POST"])
def auth():
    """Verify a license code and return user info + plan.

    Rate-limited: 5 failed attempts per IP → 5-minute block.
    """
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()

    # Rate limit check
    allowed, retry_after = _check_rate_limit(ip)
    if not allowed:
        log.warning("[aven] /auth blocked for IP %s (%ds remaining)", ip, retry_after)
        return jsonify({
            "valid": False,
            "reason": "rate_limited",
            "retry_after": retry_after,
            "message": f"تم حظر هذا العنوان مؤقتاً بسبب محاولات كثيرة. حاول بعد {retry_after} ثانية.",
        }), 429

    body = request.get_json(silent=True) or {}
    code = (body.get("code") or "").strip()
    if not code:
        _record_failed_attempt(ip)
        return jsonify({"valid": False, "reason": "not_found"}), 200

    user = _find_user_by_code(code)
    if not user:
        _record_failed_attempt(ip)
        return jsonify({"valid": False, "reason": "not_found"}), 200

    if int(user.get("is_subscribed", 0) or 0) != 1:
        return jsonify({"valid": False, "reason": "inactive"}), 200

    # Check subscription_end >= today
    end_str = (user.get("subscription_end") or "").strip()
    if end_str:
        try:
            end_date = datetime.strptime(end_str, "%Y-%m-%d").date()
            if end_date < date.today():
                _deactivate_user(code, "expired")
                return jsonify({"valid": False, "reason": "subscription_expired"}), 200
        except ValueError:
            pass  # ignore malformed date

    # Check tokens_used < tokens_limit
    used = int(user.get("tokens_used", 0) or 0)
    limit = int(user.get("tokens_limit", 0) or 0)
    if limit > 0 and used >= limit:
        _deactivate_user(code, "exhausted")
        return jsonify({"valid": False, "reason": "tokens_exhausted"}), 200

    # All checks passed — clear the fail counter for this IP
    _record_success(ip)

    return jsonify({
        "valid": True,
        "name": user.get("name", ""),
        "username": user.get("username", ""),
        "plan": _derive_plan(end_str),
        "tokens_used": used,
        "tokens_limit": limit,
        "tokens_remaining": max(0, limit - used),
        "subscription_end": end_str,
    }), 200


def _derive_plan(end_str):
    """Heuristic plan label from the subscription end date (informational only)."""
    if not end_str:
        return "lifetime"
    try:
        end_date = datetime.strptime(end_str, "%Y-%m-%d").date()
        delta_days = (end_date - date.today()).days
        if delta_days >= 80:
            return "90days"
        if delta_days >= 25:
            return "30days"
        if delta_days >= 5:
            return "7days"
        return "1day"
    except ValueError:
        return "lifetime"


# ---------- Registration endpoint ----------

# Free trial configuration: 7 days + generous token allowance
TRIAL_DAYS = 7
TRIAL_TOKEN_LIMIT = 1_000_000  # 1M tokens — more than enough for 7 days of normal use


@app.route("/register", methods=["POST"])
def register():
    """Register a new user and grant a free 7-day trial.

    Body (JSON): {name, username, contact}
    Returns: {ok: true, code, user: {...}} on success
             {ok: false, error: "..."} on failure

    Rate-limited (same IP-based lockout as /auth) to prevent abuse.
    Username must be unique (case-insensitive).
    """
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()

    allowed, retry_after = _check_rate_limit(ip)
    if not allowed:
        log.warning("[aven] /register blocked for IP %s (%ds remaining)", ip, retry_after)
        return jsonify({
            "ok": False,
            "error": "rate_limited",
            "retry_after": retry_after,
            "message": f"تم حظر هذا العنوان مؤقتاً. حاول بعد {retry_after} ثانية.",
        }), 429

    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    username = (body.get("username") or "").strip()
    contact = (body.get("contact") or "").strip()

    if not name or not username:
        return jsonify({"ok": False, "error": "name_and_username_required",
                        "message": "الاسم واسم المستخدم مطلوبان"}), 200

    with _CSV_LOCK:
        users = _read_users()

        # Check username uniqueness (case-insensitive)
        for u in users:
            if u.get("username", "").lower() == username.lower():
                _record_failed_attempt(ip)
                return jsonify({"ok": False, "error": "username_taken",
                                "message": "اسم المستخدم محجوز. اختر اسماً آخر."}), 200

        # Generate a unique code
        from admin import _gen_code
        code = _gen_code()
        while any(u.get("code") == code for u in users):
            code = _gen_code()

        end_date = (date.today() + timedelta(days=TRIAL_DAYS)).strftime("%Y-%m-%d")

        new_user = {
            "name": name,
            "username": username,
            "contact": contact,
            "code": code,
            "is_subscribed": 1,
            "tokens_used": 0,
            "tokens_limit": TRIAL_TOKEN_LIMIT,
            "subscription_end": end_date,
        }
        users.append(new_user)
        _write_users(users)

    _record_success(ip)
    log.info("[aven] New user registered: name=%s username=%s code=%s trial_ends=%s tokens=%d",
             name, username, code, end_date, TRIAL_TOKEN_LIMIT)

    return jsonify({
        "ok": True,
        "code": code,
        "user": {
            "name": name,
            "username": username,
            "plan": "7days",
            "tokens_used": 0,
            "tokens_limit": TRIAL_TOKEN_LIMIT,
            "tokens_remaining": TRIAL_TOKEN_LIMIT,
            "subscription_end": end_date,
        }
    }), 200


# ---------- Usage endpoint ----------

@app.route("/usage", methods=["GET"])
def usage():
    """Return current usage stats for the user identified by the Authorization header."""
    code = _extract_code_from_request()
    if not code:
        return jsonify({"error": "Authorization header required"}), 401
    user = _find_user_by_code(code)
    if not user:
        return jsonify({"error": "user not found"}), 404
    used = int(user.get("tokens_used", 0) or 0)
    limit = int(user.get("tokens_limit", 0) or 0)
    return jsonify({
        "name": user.get("name", ""),
        "username": user.get("username", ""),
        "tokens_used": used,
        "tokens_limit": limit,
        "tokens_remaining": max(0, limit - used),
        "is_subscribed": int(user.get("is_subscribed", 0) or 0),
        "subscription_end": user.get("subscription_end", ""),
    }), 200


# ---------- Token counting wrapper around the existing chat endpoint ----------

# The endpoint is registered as `chat_completions` (Flask uses the function name)
_original_chat_view = app.view_functions.get("chat_completions")


@app.before_request
def _aven_enforce_user_limits():
    """Run before every /v1/chat/completions request.

    Identifies the user from the Authorization header, checks subscription +
    token limits, and either blocks the request or lets it through (counting
    tokens after it completes via an after_request hook for non-streaming,
    or via a wrapper for streaming).
    """
    if request.path != "/v1/chat/completions" or request.method != "POST":
        return None  # only enforce on chat completions

    code = _extract_code_from_request()
    if not code:
        return _limit_reached_response("لم يتم توفير رمز الترخيص. يرجى تفعيل التطبيق."), 200

    user = _find_user_by_code(code)
    if not user:
        return _limit_reached_response("الرمز غير موجود. يرجى التواصل مع الدعم."), 200

    # Check subscription status
    if int(user.get("is_subscribed", 0) or 0) != 1:
        return _limit_reached_response("اشتراكك موقوف. يرجى تجديد الاشتراك."), 200

    # Check subscription expiry
    end_str = (user.get("subscription_end") or "").strip()
    if end_str:
        try:
            end_date = datetime.strptime(end_str, "%Y-%m-%d").date()
            if end_date < date.today():
                _deactivate_user(code, "expired")
                return _limit_reached_response("انتهى اشتراكك. يرجى التجديد."), 200
        except ValueError:
            pass

    # Check token limit
    used = int(user.get("tokens_used", 0) or 0)
    limit = int(user.get("tokens_limit", 0) or 0)
    if limit > 0 and used >= limit:
        _deactivate_user(code, "exhausted")
        return _limit_reached_response("لقد استنفدت رصيدك من الرموز. يرجى تجديد اشتراكك."), 200

    # Stash the code + current usage so the after_request hook can count tokens
    g.aven_code = code
    g.aven_prompt_text = _extract_prompt_text()
    return None  # allow the request to proceed


def _extract_prompt_text():
    """Concatenate all message contents from the request body for token estimation."""
    try:
        body = request.get_json(force=True, silent=True)
        if not body or not isinstance(body, dict):
            return ""
        parts = []
        for m in body.get("messages", []):
            content = m.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        parts.append(part.get("text", ""))
        return " ".join(parts)
    except Exception:
        return ""


# Wrap the original chat view so we can count response tokens
def _wrapped_chat_view(*args, **kwargs):
    """Wrap the original /v1/chat/completions handler to count tokens after."""
    response = _original_chat_view(*args, **kwargs)

    code = getattr(g, "aven_code", None)
    if not code:
        return response

    # Estimate prompt tokens
    prompt_text = getattr(g, "aven_prompt_text", "") or ""
    prompt_tokens = _estimate_tokens(prompt_text)

    # Try to extract completion text from the response
    completion_text = ""
    try:
        if isinstance(response, Response):
            # Non-streaming JSON response
            if response.mimetype and "json" in response.mimetype:
                data = response.get_json(silent=True)
                if data and isinstance(data, dict):
                    choices = data.get("choices", [])
                    if choices:
                        msg = choices[0].get("message", {})
                        completion_text = msg.get("content", "") or ""
            # Streaming responses are harder — we'd need to wrap the generator.
            # For now, count only prompt tokens for streaming; the upstream
            # already logs the final text via store.update_entry.
        elif isinstance(response, tuple) and len(response) >= 1 and isinstance(response[0], Response):
            r = response[0]
            if r.mimetype and "json" in r.mimetype:
                data = r.get_json(silent=True)
                if data and isinstance(data, dict):
                    choices = data.get("choices", [])
                    if choices:
                        msg = choices[0].get("message", {})
                        completion_text = msg.get("content", "") or ""
    except Exception as e:
        log.warning("Token counting: failed to extract completion: %s", e)

    completion_tokens = _estimate_tokens(completion_text)
    total = prompt_tokens + completion_tokens

    # Add to user's tokens_used
    updated = _add_tokens_to_user(code, total)
    if updated:
        log.info("[aven] user=%s +tokens=%d total_used=%d/%d",
                 code, total, updated.get("tokens_used", 0), updated.get("tokens_limit", 0))

    return response


# Replace the original view with the wrapper
if _original_chat_view and _original_chat_view is not _wrapped_chat_view:
    app.view_functions["chat_completions"] = _wrapped_chat_view


# ---------- Root + health ----------

@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "name": "Aven LLM Proxy",
        "version": "1.0.0",
        "endpoints": {
            "auth": "POST /auth",
            "register": "POST /register (free 7-day trial)",
            "usage": "GET /usage",
            "chat": "POST /v1/chat/completions",
            "admin": "GET /admin/",
            "models": "GET /v1/models",
        },
        "admin_panel": "http://vigorous-wildflower-64000.pktriot.xyz/admin/ (admin/admin)",
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


# ---------- Entrypoint ----------

if __name__ == "__main__":
    port = int(os.environ.get("AVEN_PORT", "8000"))
    log.info("Starting Aven LLM proxy on port %d ...", port)
    log.info("  Admin panel: http://localhost:%d/admin/  (admin/admin)", port)
    log.info("  Auth:        POST http://localhost:%d/auth", port)
    log.info("  Usage:       GET  http://localhost:%d/usage", port)
    log.info("  Chat:        POST http://localhost:%d/v1/chat/completions", port)
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
