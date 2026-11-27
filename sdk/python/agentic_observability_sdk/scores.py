"""
Scores on a trace, confirmed (CONTRACT.md §8.4, SDK 0.5.0): user feedback, evaluations, anything that judges a
finished trace, sent with LangFuse's public REST API.

Why not ``Langfuse.create_score()``: it only queues the score and never says whether LangFuse took it, and the
4.x client can't delete a score. Here each call is one HTTP request that returns True only when LangFuse accepted
it, so a caller can record what reached LangFuse and resend the rest. Standard library only: no ``langfuse``
import, so a service that doesn't trace can still score.

    from agentic_observability_sdk import score_trace, delete_score, score_id_for

    sid = score_id_for(trace_id, voter, "user_feedback")       # the same inputs give the same id
    score_trace(trace_id, "user_feedback", 0, data_type="BOOLEAN", score_id=sid, comment="…")
    delete_score(score_id_for(trace_id, voter, "feedback_reason", "Incomplete"))

- A score sent again with the same ``score_id`` **replaces** the earlier one (LangFuse upserts by id).
- Reads LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY (the project the score belongs to: the trace's
  own) and DEPLOY_ENV (the score's environment, as the tracer sets it on traces, so LangFuse's environment
  filter shows them together).
- Never raises. Without the keys, or on any failure (LangFuse down, 4xx/5xx, a timeout), returns False and logs a
  warning. Blocking: call it from a background task, not on a request's path.
"""

import base64
import json
import logging
import os
import urllib.error
import urllib.request
import uuid

logger = logging.getLogger(__name__)

DATA_TYPES = ("NUMERIC", "CATEGORICAL", "BOOLEAN")
# Fixed namespace: score_id_for() must give the same id in every process and release, so a later call replaces.
_NAMESPACE = uuid.UUID("6f1d3a4e-2b5c-4d7e-9f80-a1b2c3d4e5f6")


def score_id_for(*parts) -> str:
    """A deterministic score id (a UUID) from its parts, e.g. (trace_id, voter, name[, category])."""
    return str(uuid.uuid5(_NAMESPACE, "\x1f".join(str(p) for p in parts)))


def _config():
    host = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
    public, secret = os.environ.get("LANGFUSE_PUBLIC_KEY", ""), os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not (host and public and secret):
        return None
    token = base64.b64encode(f"{public}:{secret}".encode()).decode()
    return host, {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def _request(method: str, path: str, body=None, timeout: float = 5.0):
    cfg = _config()
    if cfg is None:
        logger.warning("LangFuse score %s skipped: LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set",
                       method)
        return None
    host, headers = cfg
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(host + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        logger.warning("LangFuse score %s %s refused: HTTP %s", method, path.split("/")[-1], e.code)
        return e.code
    except Exception as e:   # down, DNS, timeout: never the caller's problem
        logger.warning("LangFuse score %s failed: %s", method, type(e).__name__)
        return None


def score_trace(trace_id: str, name: str, value, *, data_type: str, score_id: "str | None" = None,
                comment: "str | None" = None, metadata: "dict | None" = None, timeout: float = 5.0) -> bool:
    """Score one trace. True when LangFuse accepted it (HTTP 2xx).

    ``value``: a number (NUMERIC; BOOLEAN as 1/0) or a string (CATEGORICAL). ``score_id``: pass one (e.g.
    ``score_id_for(...)``) to make a later call replace this score instead of adding another.
    """
    if data_type not in DATA_TYPES:
        raise ValueError(f"data_type must be one of {DATA_TYPES}")   # a programming error, not a runtime one
    if not trace_id:
        return False
    if data_type == "BOOLEAN":
        value = 1 if value else 0
    body = {"traceId": trace_id, "name": name, "value": value, "dataType": data_type,
            "environment": os.environ.get("DEPLOY_ENV", "local")}
    if score_id:
        body["id"] = score_id
    if comment:
        body["comment"] = comment
    if metadata:
        body["metadata"] = metadata
    status = _request("POST", "/api/public/scores", body, timeout)
    return status is not None and 200 <= status < 300


def delete_score(score_id: str, timeout: float = 5.0) -> bool:
    """Delete one score by id. True when LangFuse accepted the deletion (it answers 202 for unknown ids too)."""
    if not score_id:
        return False
    status = _request("DELETE", f"/api/public/scores/{score_id}", None, timeout)
    return status is not None and 200 <= status < 300
