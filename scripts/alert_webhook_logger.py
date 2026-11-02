"""
Minimal Alertmanager webhook receiver — stdlib only, no dependencies.

Purpose: give the local demo stack a real, working alert *destination*
without requiring a Slack/PagerDuty account. Alertmanager POSTs its webhook
payload here; this prints each firing/resolved alert to stdout so you can
watch alerts arrive with:

    docker compose -f docker-compose/docker-compose.yml --env-file .env logs -f alert-webhook-logger

Payload shape: https://prometheus.io/docs/alerting/latest/configuration/#webhook_config
"""

import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 9096


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence default per-request access log; we print our own summary

    def do_GET(self):
        if self.path == "/healthz":
            self._respond(200, b"ok")
        else:
            self._respond(404, b"not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            print(f"[{_ts()}] !! could not parse webhook payload: {raw[:200]!r}")
            self._respond(400, b"bad json")
            return

        alerts = payload.get("alerts", [])
        status = payload.get("status", "unknown")
        print(f"\n[{_ts()}] === Alertmanager webhook: {len(alerts)} alert(s), group status={status} ===")
        for a in alerts:
            name = a.get("labels", {}).get("alertname", "?")
            severity = a.get("labels", {}).get("severity", "?")
            agent = a.get("labels", {}).get("agent_name", "?")
            state = a.get("status", "?")
            summary = a.get("annotations", {}).get("summary", "")
            description = a.get("annotations", {}).get("description", "")
            marker = "🔥 FIRING  " if state == "firing" else "✅ RESOLVED"
            print(f"  {marker} [{severity:7s}] {name}  agent={agent}")
            if summary:
                print(f"             {summary}")
            if description:
                print(f"             {description.strip()}")
        self._respond(200, b"ok")

    def _respond(self, code: int, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print(f"[{_ts()}] alert-webhook-logger listening on :{PORT} (POST /alert, GET /healthz)")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
