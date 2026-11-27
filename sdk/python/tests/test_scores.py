"""score_trace / delete_score / score_id_for (SDK 0.5.0): scores sent with LangFuse's REST API, confirmed.

A fake LangFuse (http.server on a free port) records what arrives; no network. Run from sdk/python: pytest tests
"""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agentic_observability_sdk import scores


class _Fake:
    def __init__(self, status=200):
        self.status, self.seen = status, []
        fake = self

        class H(BaseHTTPRequestHandler):
            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                fake.seen.append((self.command, self.path, self.headers.get("Authorization"), body))
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")
            do_POST = do_DELETE = _handle

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def langfuse(monkeypatch):
    fake = _Fake()
    monkeypatch.setenv("LANGFUSE_HOST", fake.url + "/")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("DEPLOY_ENV", "vm")
    yield fake
    fake.close()


def test_a_score_is_posted_with_its_id_environment_and_auth(langfuse):
    sid = scores.score_id_for("t" * 32, "device-1", "user_feedback")
    assert scores.score_trace("t" * 32, "user_feedback", False, data_type="BOOLEAN", score_id=sid,
                              comment="left out a step", metadata={"turn": 2}) is True
    [(method, path, auth, body)] = langfuse.seen
    assert (method, path) == ("POST", "/api/public/scores")
    assert auth == "Basic " + base64.b64encode(b"pk-test:sk-test").decode()
    assert body == {"traceId": "t" * 32, "name": "user_feedback", "value": 0, "dataType": "BOOLEAN",
                    "environment": "vm", "id": sid, "comment": "left out a step", "metadata": {"turn": 2}}


def test_categorical_values_pass_through_and_empty_fields_are_left_out(langfuse):
    assert scores.score_trace("a" * 32, "feedback_reason", "Incomplete", data_type="CATEGORICAL") is True
    body = langfuse.seen[0][3]
    assert body["value"] == "Incomplete" and "id" not in body and "comment" not in body and "metadata" not in body


def test_delete(langfuse):
    assert scores.delete_score("8f1c2b3a-4d5e-5f60-8a7b-9c0d1e2f3a4b") is True
    assert langfuse.seen[0][:2] == ("DELETE", "/api/public/scores/8f1c2b3a-4d5e-5f60-8a7b-9c0d1e2f3a4b")


def test_score_ids_are_stable_and_distinct():
    a = scores.score_id_for("t1", "dev", "feedback_reason", "Incomplete")
    assert a == scores.score_id_for("t1", "dev", "feedback_reason", "Incomplete")       # same inputs, same id
    assert a != scores.score_id_for("t1", "dev", "feedback_reason", "Too long or unclear")
    assert a != scores.score_id_for("t1", "dev2", "feedback_reason", "Incomplete")
    assert a != scores.score_id_for("t1dev", "", "feedback_reason", "Incomplete")       # parts can't run together


@pytest.mark.parametrize("status", [400, 401, 500])
def test_a_refusal_is_false_not_an_exception(langfuse, status):
    langfuse.status = status
    assert scores.score_trace("t" * 32, "x", 1, data_type="NUMERIC") is False


def test_langfuse_down_is_false_not_an_exception(monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:9")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    assert scores.score_trace("t" * 32, "x", 1, data_type="NUMERIC", timeout=2) is False
    assert scores.delete_score("some-id", timeout=2) is False


def test_without_keys_nothing_is_sent(monkeypatch):
    for k in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert scores.score_trace("t" * 32, "x", 1, data_type="NUMERIC") is False


def test_no_trace_id_sends_nothing(langfuse):
    assert scores.score_trace("", "x", 1, data_type="NUMERIC") is False and langfuse.seen == []


def test_an_unknown_data_type_is_a_programming_error(langfuse):
    with pytest.raises(ValueError):
        scores.score_trace("t" * 32, "x", "y", data_type="TEXTY")


def test_scores_import_without_langfuse():
    from test_logs import run
    out = run("""
        import importlib.abc
        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name == "langfuse" or name.startswith("langfuse."):
                    raise ImportError("blocked")
        sys.meta_path.insert(0, Block())
        from agentic_observability_sdk import score_trace, delete_score, score_id_for
        print(json.dumps({"ok": callable(score_trace), "langfuse_loaded": "langfuse" in sys.modules}))
    """)
    assert out == {"ok": True, "langfuse_loaded": False}
