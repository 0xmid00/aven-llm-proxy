"""
store.py
--------
Minimal in-memory request log store used by proxy.py.

The original proxy.py expected a richer store with stats/dashboard support;
this implementation provides just enough for the proxy to run standalone:
  - new_entry(...)         → create a log entry, return its id
  - update_entry(id, ...)  → patch fields on an entry
  - get_all(limit)         → return last N entries (newest first)
  - stats()                → basic counts
  - clear()                → wipe all entries

All operations are thread-safe (the proxy runs with threaded=True).
"""

import threading
import time
import uuid
from collections import deque

_lock = threading.Lock()
_entries = deque(maxlen=500)  # keep at most 500 recent entries


def new_entry(method, path, raw_request_body, model, stream, num_messages, num_files):
    entry_id = uuid.uuid4().hex
    entry = {
        "id": entry_id,
        "ts": time.time(),
        "method": method,
        "path": path,
        "raw_request_body": raw_request_body,
        "model": model,
        "stream": stream,
        "num_messages": num_messages,
        "num_files": num_files,
        "status": "pending",
        "error": None,
        "chat_text_sent": None,
        "context_sent": None,
        "upstream_raw_lines": [],
        "final_response_text": None,
        "duration_ms": None,
    }
    with _lock:
        _entries.append(entry)
    return entry_id


def update_entry(entry_id, **fields):
    with _lock:
        for e in _entries:
            if e["id"] == entry_id:
                e.update(fields)
                break


def get_all(limit=200):
    with _lock:
        items = list(_entries)
    items.reverse()  # newest first
    return items[:limit]


def stats():
    with _lock:
        total = len(_entries)
        success = sum(1 for e in _entries if e.get("status") == "success")
        error = sum(1 for e in _entries if e.get("status") == "error")
    return {"total": total, "success": success, "error": error}


def clear():
    with _lock:
        _entries.clear()
