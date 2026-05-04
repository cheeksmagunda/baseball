"""Structured JSON logging for Ben Oracle.

Every log record is emitted as a single JSON line:

  {"ts":"2026-04-28T19:05:00","level":"INFO","logger":"app.services.pipeline",
   "msg":"T-65 pipeline complete","request_id":"a1b2c3d4"}

`request_id` is propagated via contextvars.  RequestIDMiddleware (wired in
main.py) sets it per HTTP request so every downstream log line from a single
request shares the same ID, making Railway log search trivial.

Background tasks (slate_monitor, Statcast refresh) that run outside an HTTP
request see request_id="-" until `set_pipeline_run_id()` is called at the
top of `run_full_pipeline`, which mints a fresh ID so every downstream log
line + outbound HTTP request from a single T-65 fire shares the same
correlation ID.

The same `request_id_var` value is also injected as the `X-Request-ID`
header on every outbound httpx call (see `tracing_event_hook`), achieving
distributed tracing without an OpenTelemetry SDK dependency.  When MLB,
RotoWire, The Odds API, or Open-Meteo log the inbound request, the
correlation ID is the join key from our log line through their logs.
"""
import json
import logging
import uuid
from contextvars import ContextVar

import httpx

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


def set_pipeline_run_id() -> tuple[str, "object"]:
    """Mint a fresh correlation ID and set it as the active `request_id_var`.

    Call at the top of `run_full_pipeline` so every log line and outbound
    HTTP request inside the T-65 fire shares the same correlation ID.
    Background tasks have no FastAPI middleware to do this for them.

    Returns (rid, reset_token).  Pass the token to `request_id_var.reset()`
    in a `finally` block so the var doesn't bleed into subsequent code that
    runs in the same asyncio task — mirrors the
    `_RequestIDMiddleware.dispatch` pattern in `app/main.py`.
    """
    rid = uuid.uuid4().hex[:8]
    token = request_id_var.set(rid)
    return rid, token


async def tracing_event_hook(request: httpx.Request) -> None:
    """Httpx request event hook — attach the active correlation ID as
    `X-Request-ID` on every outbound call.

    Wired into every module-level `httpx.AsyncClient` (mlb_api, odds_api,
    open_meteo, rotowire) via `event_hooks={"request": [tracing_event_hook]}`.
    Reading from `request_id_var` keeps the hook free of import-time cycles
    and inherits whatever ID the FastAPI middleware (or
    `set_pipeline_run_id()`) set for this asyncio task.

    Skips when no ID is set (default "-") so we don't emit a meaningless
    header for shell-invoked one-off scripts.
    """
    rid = request_id_var.get()
    if rid and rid != "-":
        request.headers["X-Request-ID"] = rid
