# Contributing

Thanks for your interest in improving this sample!

## Ways to help

- **Bug reports**: open an issue with what you did, what you expected and what happened (the runtime logs from the AgentBase console, or `docker compose logs` for the MCP server, help a lot).
- **Feature ideas**: keep the sample *minimal*: it exists to teach the GreenNode AgentBase platform (Runtime + Memory + MCP Gateway + Policy), not to be a full product.
- **Docs**: clarity fixes to the README and the deploy guides are very welcome.

## Pull requests

1. Fork and branch: `feat/my-change`.
2. The backend stays **no-build** Python (SDK `greennode-agentbase`) and the frontend stays **vanilla** (no framework, no CDN). Use Python 3.10+ (3.12 recommended, it is what the images use).
3. Run the checks locally. Install **both** requirement files, because the tests import the agent backend and the MCP server:

   ```bash
   pip install -r src/backend/requirements.txt -r src/mcp-server/requirements.txt pytest ruff
   pytest -q
   ruff check --select F,E9,B,UP,SIM --target-version py312 src tests
   node --check src/frontend/app.js
   bash -n deploy/check_connectivity.sh    # and shellcheck deploy/check_connectivity.sh if you have it
   ```

4. Keep secrets out: `.env` and `.env.*` are git-ignored (only `.env.example` files are committed): never commit tokens or API keys, and keep example secrets obviously invalid.
5. Code comments, logs, docs and `.env.example` files are in English. The text a guest reads in Zalo (and the simulator's UI strings) may stay Vietnamese.
6. PRs should pass CI (ruff, pytest, both image builds, compose config and shell checks) before review.
