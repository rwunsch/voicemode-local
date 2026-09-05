"""Does 8.12.0 still need patch_converse_cancel.py?

Upstream VM-2015 (8.12.0) claims CancelledError is now handled at the boundary
the MCP SDK expects. The 8.7.1 bug was a handler that CAUGHT CancelledError and
RETURNED a normal result -- under fastmcp 3.x the SDK has already responded, so
the second response trips `assert not self._completed` and kills the server.

A fixed version must re-raise (or not catch at all).
"""
import ast


def _cancelled_handlers(src: str):
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        if "CancelledError" not in ast.dump(node.type):
            continue
        returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
        raises = [n for n in ast.walk(node) if isinstance(n, ast.Raise)]
        out.append((node.lineno, len(returns), len(raises)))
    return out


def test_cancellederror_is_reraised_not_swallowed(converse_src):
    handlers = _cancelled_handlers(converse_src)
    assert handlers, "no CancelledError handler found at all - upstream restructured"
    swallowers = [(ln, r, x) for (ln, r, x) in handlers if r and not x]
    assert not swallowers, (
        f"CancelledError handler(s) return without re-raising at line(s) "
        f"{[ln for ln, _, _ in swallowers]} - patch_converse_cancel.py STILL NEEDED"
    )
