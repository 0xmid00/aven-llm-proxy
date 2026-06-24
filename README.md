# Aven LLM Proxy

A self-contained Python proxy that exposes an OpenAI-compatible API while enforcing per-user subscription limits, token counting, and an admin panel for managing users.

`python run.py` starts everything on a single port (`8000`):

| Endpoint | Purpose |
|---|---|
| `POST /v1/chat/completions` | OpenAI-compatible chat (with token counting + sub enforcement) |
| `GET  /v1/models` | List available models |
| `POST /auth` | Verify a license code → return user info + plan |
| `GET  /usage` | Return current user's token usage stats |
| `GET  /admin/` | Web admin panel (login: `admin` / `admin`) |
| `POST /admin/add_user` | Create a user (code auto-generated `AVEN-XXXX-XXXX`) |
| `POST /admin/edit_user` | Edit any field on a user |
| `POST /admin/delete_user` | Delete a user by code |
| `POST /admin/toggle_sub` | Toggle `is_subscribed` 0/1 |

---

## Installation

```bash
cd llm-proxy
pip install -r requirements.txt
```

## Run

```bash
python run.py
```

- **Admin panel**: http://vigorous-wildflower-64000.pktriot.xyz/admin/  (login `admin` / `admin`)
- **API base URL** (for the Aven extension or any OpenAI client): `http://vigorous-wildflower-64000.pktriot.xyz/v1`

---

## users.csv schema

```
name, username, contact, code, is_subscribed, tokens_used, tokens_limit, subscription_end
```

Example:

```
Ahmed, ahmed99, +213555123456, AVEN-XK92-PLMQ, 1, 45200, 100000, 2026-08-01
```

- `code` — unique license code (`AVEN-XXXX-XXXX` format, or `ahmed2001` for the hardcoded admin)
- `is_subscribed` — `1` (active) or `0` (inactive); auto-set to `0` when tokens exhausted or subscription expired
- `tokens_used` / `tokens_limit` — running token total vs cap; when `used >= limit`, the user is auto-deactivated
- `subscription_end` — `YYYY-MM-DD`; if before today, the user is auto-deactivated on the next request

---

## Endpoints

### POST /auth — Verify a license code

**Request**
```
POST /auth
Content-Type: application/json

{ "code": "AVEN-XK92-PLMQ" }
```

**Response (valid)**
```json
{
  "valid": true,
  "name": "Ahmed",
  "username": "ahmed99",
  "plan": "30days",
  "tokens_used": 45200,
  "tokens_limit": 100000,
  "tokens_remaining": 54800,
  "subscription_end": "2026-08-01"
}
```

**Response (invalid)** — `reason` is one of: `not_found`, `inactive`, `subscription_expired`, `tokens_exhausted`
```json
{ "valid": false, "reason": "subscription_expired" }
```

---

### GET /usage — Current user's token usage

**Request**
```
GET /usage
Authorization: Bearer AVEN-XK92-PLMQ
```

**Response**
```json
{
  "name": "Ahmed",
  "username": "ahmed99",
  "tokens_used": 45200,
  "tokens_limit": 100000,
  "tokens_remaining": 54800,
  "is_subscribed": 1,
  "subscription_end": "2026-08-01"
}
```

---

### POST /v1/chat/completions — Chat (OpenAI-compatible)

**Request**
```
POST /v1/chat/completions
Authorization: Bearer AVEN-XK92-PLMQ
Content-Type: application/json

{
  "model": "deepseek/deepseek-v4-flash-20260423",
  "messages": [{ "role": "user", "content": "Hello" }],
  "stream": false
}
```

**Behaviour**
1. Read `Authorization` header → identify user by code
2. Check `is_subscribed == 1`, `subscription_end >= today`, `tokens_used < tokens_limit`
3. If any check fails → return OpenAI-shaped response with Arabic error message, no upstream call
4. Otherwise → forward to upstream LLM, count tokens (`prompt_tokens + completion_tokens`, estimated as `words × 1.3`), add to `tokens_used` in users.csv
5. If `tokens_used >= tokens_limit` after the call → auto-set `is_subscribed = 0`

**Response (success)** — standard OpenAI chat completion shape:
```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1719000000,
  "model": "deepseek-v4-flash-20260423",
  "choices": [{
    "index": 0,
    "message": { "role": "assistant", "content": "..." },
    "finish_reason": "stop"
  }],
  "usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0 }
}
```

**Response (limit reached)** — same shape, Arabic message:
```json
{
  "id": "chatcmpl-limit",
  "object": "chat.completion",
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "لقد استنفدت رصيدك من الرموز. يرجى تجديد اشتراكك."
    },
    "finish_reason": "stop"
  }],
  "usage": { "total_tokens": 0 }
}
```

---

### GET /admin/ — Admin panel (web UI)

Open http://vigorous-wildflower-64000.pktriot.xyz/admin/ in a browser. Login: `admin` / `admin`.

The dashboard shows a user table with inline-editable fields and an "Add user" button that auto-generates the `AVEN-XXXX-XXXX` code.

---

### POST /admin/add_user

```
POST /admin/add_user
Content-Type: application/json
Cookie: session=...

{
  "name": "Karim",
  "username": "karim.ai",
  "contact": "+213555000111",
  "tokens_limit": 100000,
  "subscription_end": "2026-12-31"
}
```

**Response**
```json
{
  "ok": true,
  "user": {
    "name": "Karim",
    "username": "karim.ai",
    "contact": "+213555000111",
    "code": "AVEN-7K3P-9M2X",
    "is_subscribed": 1,
    "tokens_used": 0,
    "tokens_limit": 100000,
    "tokens_remaining": 100000,
    "subscription_end": "2026-12-31"
  }
}
```

---

### POST /admin/edit_user

```
POST /admin/edit_user
Content-Type: application/json

{
  "code": "AVEN-XK92-PLMQ",
  "tokens_limit": 200000,
  "subscription_end": "2027-01-15"
}
```

Any subset of fields may be provided; the rest stay unchanged.

---

### POST /admin/delete_user

```
POST /admin/delete_user
Content-Type: application/json

{ "code": "AVEN-XK92-PLMQ" }
```

---

## Project structure

```
llm-proxy/
  run.py           ← entry point — starts everything on port 8000
  proxy.py         ← OpenAI-compatible API surface (forwards to upstream)
  admin.py         ← admin panel (login + user CRUD) — Flask blueprint
  store.py         ← in-memory request log store (used by proxy.py)
  rules.py         ← rules loader (default pass-through; hot-reloadable rules_active.py)
  users.csv        ← user database (read + written on every change)
  requirements.txt
  README.md
```

---

## Notes

- The proxy forwards to `https://llmproxy.org/api/chat.php` by default (defined in `rules.py`). Drop a `rules_active.py` next to `rules.py` to override the upstream URL or model mapping.
- Token counting uses `words × 1.3` as a simple estimate. Install `tiktoken` and edit `_estimate_tokens` in `run.py` for accurate counts.
- All CSV writes are thread-safe (a single `threading.Lock` protects `users.csv`).
- The admin session uses Flask's signed cookie — set `AVEN_SECRET_KEY` env var to override the default secret.
- Contact: 0xmid00o@gmail.com
