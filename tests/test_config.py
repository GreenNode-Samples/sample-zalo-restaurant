"""Startup validation: missing values and the .env.example placeholders are rejected."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "src" / "backend"
GOOD_ENV = {
    "LLM_API_KEY": "lap-real", "AGENTBASE_MEMORY_ID": "memory-1", "MEMORY_STRATEGY_ID": "ltms-1",
    "MCP_RESTAURANT_URL": "https://gw.example/restaurant",
}
OPTIONAL = ("AGENT_API_KEY", "ZALO_BOT_TOKEN", "ZALO_WEBHOOK_SECRET")


def start(module: str, **overrides) -> subprocess.CompletedProcess:
    """Import a backend module in a clean interpreter with the given environment."""
    env = {k: v for k, v in os.environ.items() if k not in GOOD_ENV and k not in OPTIONAL}
    # "" and not absent: main.py's load_dotenv() must not pick up a real .env from a parent directory.
    env.update({**dict.fromkeys(OPTIONAL, ""), **GOOD_ENV, **overrides, "PYTHONPATH": str(BACKEND)})
    env = {k: v for k, v in env.items() if v is not None}
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"], env=env, cwd=BACKEND.parent.parent / "tests",
        capture_output=True, text=True, timeout=60,
    )


def test_a_complete_configuration_starts():
    assert start("agent").returncode == 0
    assert start("main").returncode == 0


@pytest.mark.parametrize(
    ("name", "value"),
    [(name, "change-me") for name in GOOD_ENV] + [("LLM_API_KEY", ""), ("MCP_RESTAURANT_URL", "change-me-url")],
)
def test_each_required_value_is_checked(name, value):
    result = start("agent", **{name: value})
    assert result.returncode != 0 and f"{name} is not configured" in result.stderr


@pytest.mark.parametrize("name", OPTIONAL)
def test_the_example_placeholder_of_an_optional_secret_is_rejected(name):
    result = start("main", **{name: "change-me"})
    assert result.returncode != 0 and f"{name} still has" in result.stderr
    assert start("main", **{name: "a-real-secret-value"}).returncode == 0


def test_only_the_repositorys_own_env_file_is_read():
    """load_dotenv() without a path walks up the tree and could load an unrelated .env of a parent folder."""
    import main

    repo = BACKEND.parent.parent
    expected = repo / ".env"
    assert expected == main.ENV_FILE
    assert (repo / "src" / "backend" / "main.py").is_file()  # the depth (parents[2]) really is the repo root
