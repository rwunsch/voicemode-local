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
