"""Application-level state shared across modules.

`startup_done_event` is an asyncio.Event set by main.py's background init
task once database migrations, Redis validation, and seed steps all
complete.  Routers and the /api/health endpoint read it to distinguish
"starting" from "ready".

Created at module-import time — asyncio.Event() is safe without a running
event loop since Python 3.10 (which this project requires via pyproject.toml).
"""
import asyncio

startup_done_event: asyncio.Event = asyncio.Event()
