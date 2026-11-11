"""MCP progress heartbeat for long converse calls (voicemode-local, 2026-11-11).

Copilot CLI cancels a tool call after 180 s ("MCP error -32001: Request timed out")
unless notifications/progress arrive; each one resets its timer (FELIX-voice, Copilot
CLI 1.0.107, n=1). A converse call can wait in the conch queue or listen for longer.
While it runs, this sends progress every VOICEMODE_PROGRESS_INTERVAL seconds
(default 30, 0 = off). It goes out only when the client asked for progress (a
progressToken on the request); otherwise fastmcp sends nothing. Never raises into
the call it wraps.
"""
import asyncio
import contextlib
import logging
import os

logger = logging.getLogger("voicemode.mcp_progress")

INTERVAL_SETTING = "VOICEMODE_PROGRESS_INTERVAL"
DEFAULT_INTERVAL_S = 30.0
MESSAGE = "voice: still working (waiting for the voice channel or listening)"


def interval_s() -> float:
    try:
        value = float(os.environ.get(INTERVAL_SETTING, DEFAULT_INTERVAL_S))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_S
    return value if value >= 0 else DEFAULT_INTERVAL_S


def _context():
    from fastmcp.server.dependencies import get_context
    return get_context()


async def _beat(ctx, interval: float) -> None:
    n = 0
    while True:
        await asyncio.sleep(interval)
        n += 1
        try:
            await ctx.report_progress(progress=n, message=MESSAGE)
        except Exception:  # noqa: BLE001 - a lost client must not end the call
            logger.debug("progress not sent", exc_info=True)


@contextlib.asynccontextmanager
async def heartbeat(ctx_getter=None, interval=None):
    try:
        ctx = (ctx_getter or _context)()
    except Exception:  # noqa: BLE001 - no request context: nothing to report to
        ctx = None
    every = interval_s() if interval is None else interval
    task = None
    if ctx is not None and every > 0:
        try:  # whether the client asked for progress decides if anything is sent
            meta = getattr(getattr(ctx, "request_context", None), "meta", None)
            logger.info("progress heartbeat: every %.0fs, client token %s", every,
                        "present" if getattr(meta, "progressToken", None) is not None else "absent")
        except Exception:  # noqa: BLE001
            pass
        try:
            task = asyncio.get_running_loop().create_task(_beat(ctx, every))
        except Exception:  # noqa: BLE001
            task = None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
