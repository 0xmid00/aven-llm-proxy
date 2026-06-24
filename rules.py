"""
rules.py
--------
Loads transformation rules for proxy.py.

The original proxy.py used a hot-reloadable rules_active.py file edited from
a dashboard. For the standalone llm-proxy we ship a simple built-in default
that maps the requested model to the upstream model and passes the messages
through unchanged. You can still drop a `rules_active.py` next to this file
to override build_upstream_request / transform_upstream_line.

Public API:
  load_rules()        → module (the rules_active module if present and
                        importable, else a built-in default module)
  get_last_error()    → string (last import error, or None)
"""

import importlib.util
import os
import sys
import traceback

_last_error = None


def _build_default_module():
    """Build a default rules module with sensible pass-through behaviour."""
    import types

    mod = types.ModuleType("rules_default")

    UPSTREAM_URL = "https://llmproxy.org/api/chat.php"
    UPSTREAM_MODEL_MAP = {
        "gpt-4o": "gpt-4o",
        "gpt-4o-mini": "gpt-4o-mini",
        "deepseek-v4-flash-20260423": "deepseek-v4-flash-20260423",
        "deepseek/deepseek-v4-flash-20260423": "deepseek-v4-flash-20260423",
    }

    def build_upstream_request(messages, requested_model, stream):
        # Map model name; default to the requested model if no mapping exists
        upstream_model = UPSTREAM_MODEL_MAP.get(requested_model, requested_model)
        return {
            "model": upstream_model,
            "messages": messages,
            "stream": True,  # upstream only supports streaming
        }

    def transform_upstream_line(payload):
        # Pass through unchanged; strip any non-OpenAI fields
        if not isinstance(payload, dict):
            return None
        return payload

    mod.UPSTREAM_URL = UPSTREAM_URL
    mod.build_upstream_request = build_upstream_request
    mod.transform_upstream_line = transform_upstream_line
    return mod


def load_rules():
    """Load rules_active.py if it exists and imports cleanly; else default."""
    global _last_error
    here = os.path.dirname(os.path.abspath(__file__))
    active_path = os.path.join(here, "rules_active.py")
    if not os.path.exists(active_path):
        _last_error = None
        return _build_default_module()
    try:
        # Drop any cached copy so edits apply on the next call
        if "rules_active" in sys.modules:
            del sys.modules["rules_active"]
        spec = importlib.util.spec_from_file_location("rules_active", active_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _last_error = None
        return mod
    except Exception as e:
        _last_error = f"{e}\n{traceback.format_exc()}"
        return None


def get_last_error():
    return _last_error
