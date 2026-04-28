"""Structured JSON logging for Ben Oracle.

Every log record is emitted as a single JSON line:

  {"ts":"2026-04-28T19:05:00","level":"INFO","logger":"app.services.pipeline",
   "msg":"T-65 pipeline complete","request_id":"a1b2c3d4"}

`request_id` is propagated via contextvars.  RequestIDMiddleware (wired in
main.py) sets it per HTTP request so every downstream log line from a single
request shares the same ID, making Railway log search trivial.

Background tasks (slate_monitor, Statcast refresh) that run outside an HTTP
request see request_id="-".
"""
import json
import logging
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single compact JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            out["stack"] = self.formatStack(record.stack_info)
        return json.dumps(out, default=str)
