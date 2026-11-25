"""
Logs over OTLP: Python logging -> OTel Collector -> Loki (CONTRACT.md §8, docs/LOGS-PLAN.md §2).

    from agentic_observability_sdk.logs import init_logging   # no langfuse needed
    init_logging()      # once at startup, AFTER your own logging.basicConfig / file handlers

What it does:
  - adds ONE handler to the root logger, at OTEL_LOG_LEVEL (default INFO), next to the handlers you already
    have: console and files stay as they are, the full DEBUG record on each machine;
  - sends each record over OTLP/HTTP to <OTEL_EXPORTER_OTLP_ENDPOINT>/v1/logs (or
    OTEL_EXPORTER_OTLP_LOGS_ENDPOINT, a full URL), through a background batch: it never blocks the caller,
    and when the queue is full or the Collector is down, records are dropped and counted
    (agent_log_records_dropped_total, labels reason=queue_full|export_failed, tl_project);
  - a record written inside a span (@trace_agent, a LangChain callback under it) carries that span's
    trace_id and span_id, the ids LangFuse shows; exceptions are one record with exception.stacktrace;
    ``extra={...}`` fields become attributes (Loki: structured metadata);
  - is a no-op without an endpoint, and idempotent.

This module is the only one that touches OpenTelemetry's logs API (``_logs``, still marked unstable): its
import paths move between releases, so they stay here, covered by tests/test_logs.py.

Pitfalls:
  - logging.basicConfig(force=True) AFTER init_logging() removes every root handler, this one too.
    shutdown_logging() warns when it finds its handler gone.
  - Loggers filter before handlers: init_logging() doesn't change any logger's level, so a record only
    ships if the service's own levels let it through (Python's root logger defaults to WARNING; set it,
    e.g. logging.basicConfig(level=logging.INFO), before init_logging()).
"""

import logging
import os
import threading
import warnings

from .project import PROJECT_ATTR, get_project
from .resource import build_resource

_lock = threading.Lock()
_provider = None
_handler = None

# Records from the export path itself: a failing export must not log about itself into the same queue.
_EXPORT_PATH_LOGGERS = ("opentelemetry", "urllib3.connectionpool", "requests.packages.urllib3")

DROPPED_METRIC = "agent_log_records_dropped_total"
_dropped_counter = None


class _ExportPathFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(_EXPORT_PATH_LOGGERS)


def _count_dropped(n: int, reason: str) -> None:
    """Add n to agent_log_records_dropped_total. Never raises."""
    global _dropped_counter
    try:
        if _dropped_counter is None:
            from opentelemetry import metrics as otel_metrics
            _dropped_counter = otel_metrics.get_meter("platform.agents").create_counter(
                DROPPED_METRIC, unit="1",
                description="Log records the SDK dropped before they reached the Collector")
        _dropped_counter.add(n, {"reason": reason, PROJECT_ATTR: get_project() or "unset"})
    except Exception:
        pass


def _make_exporter(endpoint: str):
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult

    class _CountingExporter(LogRecordExporter):
        """Counts the records of each batch the Collector didn't take."""

        def __init__(self, inner):
            self._inner = inner

        def export(self, batch):
            try:
                result = self._inner.export(batch)
            except Exception:
                result = LogRecordExportResult.FAILURE
            if result != LogRecordExportResult.SUCCESS:
                _count_dropped(len(batch), "export_failed")
            return result

        def shutdown(self):
            self._inner.shutdown()

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            flush = getattr(self._inner, "force_flush", None)
            return flush(timeout_millis) if flush else True

    return _CountingExporter(OTLPLogExporter(endpoint=endpoint, timeout=10))


def _make_processor(exporter):
    from opentelemetry.sdk._logs import LogRecordProcessor
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    class _CountingProcessor(LogRecordProcessor):
        """The batch processor, plus a count of what it drops when its queue is full (it only logs that)."""

        def __init__(self, inner):
            self._inner = inner

        def _queue_full(self) -> bool:
            queue = getattr(getattr(self._inner, "_batch_processor", None), "_queue", None)
            return queue is not None and queue.maxlen is not None and len(queue) >= queue.maxlen

        def on_emit(self, log_record):
            if self._queue_full():
                _count_dropped(1, "queue_full")
            self._inner.on_emit(log_record)

        def shutdown(self):
            self._inner.shutdown()

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return self._inner.force_flush(timeout_millis)

    # Never blocks the application: export runs on the processor's own thread, which it rebuilds after fork().
    return _CountingProcessor(BatchLogRecordProcessor(
        exporter,
        schedule_delay_millis=1000,
        max_export_batch_size=512,
        export_timeout_millis=10000,
        max_queue_size=2048,
    ))


def _make_handler(level: int, provider) -> logging.Handler:
    """The OTel logging handler: opentelemetry-instrumentation-logging's, else the SDK's (deprecated in 1.40)."""
    try:
        from opentelemetry.instrumentation.logging.handler import LoggingHandler
        handler = LoggingHandler(level=level, logger_provider=provider, log_code_attributes=True)
    except ImportError:
        from opentelemetry.sdk._logs import LoggingHandler
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            handler = LoggingHandler(level=level, logger_provider=provider)
    handler.addFilter(_ExportPathFilter())
    return handler


def _endpoint() -> "str | None":
    full = os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "").strip()
    if full:
        return full
    base = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    return f"{base.rstrip('/')}/v1/logs" if base else None


def _level(level) -> int:
    if level is None:
        level = os.environ.get("OTEL_LOG_LEVEL", "INFO")
    if isinstance(level, str):
        return logging.getLevelName(level.strip().upper()) if level.strip() else logging.INFO
    return int(level)


def init_logging(level=None, *, exporter=None) -> bool:
    """Ship this process's log records over OTLP. Returns True when shipping, False when not configured.

    Call once at startup, after your own logging setup. ``level`` overrides OTEL_LOG_LEVEL (default INFO;
    DEBUG stays local: the Collector drops it anyway). ``exporter`` is for tests (an in-memory exporter).
    Never raises: a failure to set up leaves the service running with its local logs only.
    """
    global _provider, _handler
    with _lock:
        if _handler is not None:
            return True
        endpoint = _endpoint()
        if exporter is None and not endpoint:
            return False
        try:
            from opentelemetry.sdk._logs import LoggerProvider

            provider = LoggerProvider(resource=build_resource())
            provider.add_log_record_processor(_make_processor(exporter or _make_exporter(endpoint)))
            # The drop counter needs a real MeterProvider (idempotent; no-op without an endpoint).
            from .metrics import init_metrics
            init_metrics()
            handler = _make_handler(_level(level), provider)
            logging.getLogger().addHandler(handler)
            _provider, _handler = provider, handler
            return True
        except Exception as exc:  # never let observability wiring crash the service
            warnings.warn(f"init_logging failed; logs stay local only: {exc!r}", stacklevel=2)
            return False


def flush_logging(timeout_millis: int = 5000) -> bool:
    """Push everything queued to the exporter now (tests, before a short-lived process exits)."""
    provider = _provider
    return provider.force_flush(timeout_millis) if provider is not None else True


def shutdown_logging() -> None:
    """Flush, shut the LoggerProvider down and remove the handler. Called by shutdown()."""
    global _provider, _handler
    with _lock:
        if _handler is None:
            return
        if _handler not in logging.getLogger().handlers:
            warnings.warn("The OTel log handler was removed from the root logger (logging.basicConfig("
                          "force=True) after init_logging()?): records stopped shipping at that point.")
        logging.getLogger().removeHandler(_handler)
        try:
            _provider.force_flush(5000)
            _provider.shutdown()
        except Exception:
            pass
        _provider, _handler = None, None
