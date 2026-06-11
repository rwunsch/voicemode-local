#!/usr/bin/env python3
"""Patch voice_mode/tools/converse.py to re-raise client cancellations.

Upstream 8.7.1 catches asyncio.CancelledError in converse() and RETURNS a
normal result ("Cancelled by user.") — a workaround written for FastMCP 2.x,
where an escaping CancelledError tore down the server. But 8.7.1 itself pins
fastmcp>=3.2,<4, and under fastmcp 3.x / mcp>=1.26 the SDK already responds to
the cancelled request the moment the client's notifications/cancelled arrives.
When the tool then returns a second result, mcp.server.lowlevel calls
message.respond() again and dies on `assert not self._completed` ("Request
already responded to"), killing the whole MCP server process. The next
converse call in that session fails with "MCP error -32000: Connection
closed". (Often delayed: the crash fires only when the cancelled
recording/STT finishes and the handler returns.)

The fix inverts the workaround: keep the TOOL_CANCELLED logging, then
re-raise. mcp.server.lowlevel catches the cancellation
(`except anyio.get_cancelled_exc_class(): return`) and suppresses the
duplicate response cleanly; converse's `finally` block (queue-floor release,
TOOL_REQUEST_END) still runs.

The except block is replaced between two stable anchors. Idempotent; fails
loudly if the anchors drift. Verified against voice-mode 8.7.1.

Usage: patch_converse_cancel.py [<path-to-converse.py>]
"""
import sys
from pathlib import Path

MARKER = "RE-RAISE on client cancel (voicemode-local)"

# Start: the except line itself. End: the swallow-and-return tail we remove.
B_START = "    except asyncio.CancelledError:\n"
B_END = (
    '        result = "Cancelled by user."\n'
    "        success = False\n"
    "        return result\n"
)
B_REPLACE = (
    "    except asyncio.CancelledError:\n"
    "        # Tool call was cancelled by the MCP client (e.g. user pressed ESC).\n"
    "        #\n"
    "        # RE-RAISE on client cancel (voicemode-local): upstream swallowed the\n"
    "        # cancellation and returned a result — correct under FastMCP 2.x, fatal\n"
    "        # under fastmcp 3.x / mcp>=1.26. There the SDK has already responded to\n"
    "        # the cancelled request, so returning makes the lowlevel server respond\n"
    "        # a second time and die on 'assert not self._completed', killing the\n"
    "        # whole MCP server process (next converse: MCP -32000 Connection\n"
    "        # closed). Re-raising lets mcp.server.lowlevel suppress the duplicate\n"
    "        # response; the finally block below still releases the queue floor and\n"
    "        # logs TOOL_REQUEST_END.\n"
    '        logger.info("Converse cancelled by client (ESC or tool-call cancel)")\n'
    "        if event_logger:\n"
    '            event_logger.log_event("TOOL_CANCELLED", {\n'
    '                "tool_name": "converse",\n'
    '                "reason": "client_cancel",\n'
    "            })\n"
    "        success = False\n"
    "        raise\n"
)


def apply(target: Path) -> int:
    src = target.read_text()
    if MARKER in src:
        print(f"  already patched: {target}")
        return 0
    for name, marker in (("start", B_START), ("end", B_END)):
        if src.count(marker) != 1:
            print(f"ANCHOR DRIFT: marker '{name}' matched {src.count(marker)} "
                  f"times (expected 1) in {target}. Upstream converse.py "
                  f"changed — update patch_converse_cancel.py.", file=sys.stderr)
            return 1
    si = src.index(B_START)
    ei = src.index(B_END)
    if not si < ei:
        print("ANCHOR DRIFT: start marker not before end marker.", file=sys.stderr)
        return 1
    out = src[:si] + B_REPLACE + src[ei + len(B_END):]
    compile(out, str(target), "exec")
    target.write_text(out)
    print(f"  patched (re-raise client cancel): {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        import voice_mode
        target = Path(voice_mode.__file__).parent / "tools" / "converse.py"
    sys.exit(apply(target))
