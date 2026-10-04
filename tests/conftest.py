"""Test setup: dummy environment BEFORE the backend modules are imported (agent.py validates it)."""
import os
import sys
from pathlib import Path

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")
os.environ.setdefault("AGENTBASE_MEMORY_ID", "memory-test")
os.environ.setdefault("MEMORY_STRATEGY_ID", "ltms-cust-test")
os.environ.setdefault("MCP_RESTAURANT_URL", "https://gw.example/restaurant")
# Optional settings are set to "" (not removed): main.py calls load_dotenv(), which would otherwise
# fill a missing variable from a real .env file found in a parent directory.
for name in (
    "AGENT_API_KEY", "ZALO_BOT_TOKEN", "ZALO_WEBHOOK_SECRET", "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST", "GREENNODE_CLIENT_ID", "GREENNODE_CLIENT_SECRET",
):
    os.environ[name] = ""
# Both src/backend and src/mcp-server have a main.py: only the backend is put on sys.path (the MCP
# server tests load their module by file path), so `import main` is always the backend's.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
