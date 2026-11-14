"""No file FELIX vendors imports a model provider's library (2026-11-14).

FELIX never calls a model provider directly, and its tests/repo/test_no_direct_model_calls.py fails
on any such import in a shipped file, including text a patch injects into voice-mode. v1.2.10's
patch_tts_no_double_synthesis.py injected `from openai import APITimeoutError` to recognise the
OpenAI SDK's timeout, and so could not be vendored. The pattern below is FELIX's rule for Python.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PROVIDER_MODULES = (
    "anthropic|openai|litellm|mistralai|cohere|groq|vertexai|langchain_openai|langchain_anthropic"
    "|google\\.generativeai|google\\.genai|claude_agent_sdk"
)
IMPORT = re.compile(
    rf"^[ \t]*(?:from\s+(?:{PROVIDER_MODULES})\b|from\s+google\s+import\s+(?:genai|generativeai)\b"
    rf"|import\s+(?:[\w.]+(?:\s+as\s+\w+)?\s*,\s*)*(?:{PROVIDER_MODULES})\b)"
    rf"|(?:import_module|__import__)\(\s*[\"'](?:{PROVIDER_MODULES})[\"']",
    re.M,
)
# What FELIX vendors: the patch folder (every list it runs draws from it) and the Kokoro server.
VENDORED = sorted((REPO / "patches").glob("*.py")) + [REPO / "kokoro-onnx-server.py"]


@pytest.mark.parametrize("path", VENDORED, ids=lambda p: p.name)
def test_no_model_provider_import(path):
    found = IMPORT.findall(path.read_text(encoding="utf-8"))
    assert not found, f"{path.name} imports a model provider's library: {found}"


# The helper the patch injects must still recognise the SDK's timeout without importing it.
# Runs by default (no VM812_SRC): the helper text is executed with the names core.py gives it.
def _is_tts_timeout():
    import asyncio
    import importlib.util
    import httpx
    spec = importlib.util.spec_from_file_location("nd_patch", REPO / "patches" / "patch_tts_no_double_synthesis.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = {"httpx": httpx, "asyncio": asyncio}
    exec(module.C_HELPER, names)
    return names["_vml_is_tts_timeout"]


def test_the_sdk_timeout_is_recognised_by_its_class_name():
    import asyncio
    import httpx
    is_timeout = _is_tts_timeout()

    class APIConnectionError(Exception):
        pass

    class APITimeoutError(APIConnectionError):   # the SDK's shape: a connection error subclass
        pass

    class SlowerTimeout(APITimeoutError):
        pass
    for error in (APITimeoutError(), SlowerTimeout(), httpx.ReadTimeout("t"), TimeoutError(),
                  asyncio.TimeoutError()):
        assert is_timeout(error), type(error).__name__
    for error in (APIConnectionError(), ConnectionError(), ValueError("APITimeoutError"), httpx.ConnectError("c")):
        assert not is_timeout(error), type(error).__name__
