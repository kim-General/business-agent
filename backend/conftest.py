"""Keep tests hermetic: never read the developer's real .env LLM settings.

The planner reads AGENT_MODE/LLM_* directly from os.environ (loaded from the
project .env at import time), so tests must pin the rules mode and strip
real model credentials before any test exercises plan_question.
"""

import pytest


@pytest.fixture(autouse=True)
def hermetic_environment(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "rules")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
