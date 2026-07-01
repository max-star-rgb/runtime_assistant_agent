# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**runTime** is a Python 3.11+ asyncio-based Gateway/Runtime split architecture for OpenClaw-compatible agent execution with LLM integration (Anthropic, MiniMax, Qwen). Communication uses JSON frames over WebSocket.

- **Gateway** (port 8765): Accepts external WebSocket clients, routes to Runtime, handles multi-user sessions and telephony call routing
- **Runtime** (port 8766): Executes agent logic, manages skills, integrates with LLMs, coordinates cancellation/interrupts

## Common Commands

```bash
# Install
pip install -r requirements.txt
pip install -e .

# Start Runtime (terminal 1)
python -m openclaw_gateway_runtime.runtime.ws_server

# Start Gateway (terminal 2)
python -m openclaw_gateway_runtime.gateway.ws_server

# Docker Compose
docker compose -f deploy/docker-compose.yml up --build

# Run all tests
python -m unittest discover -s tests -p "test_*.py" -v

# Run a single test file
python -m unittest tests.test_cancel_interrupt -v

# Run a single test method
python -m unittest tests.test_cancel_interrupt.CancelInterruptTests.test_cancel_stops_stream_and_emits_cancelled -v

# Interactive CLI client (requires Gateway + Runtime running)
python scripts/cli_agent_client.py

# E4 skill demo (requires LLM configured)
python scripts/e4_skill_demo.py --url ws://127.0.0.1:8765
```

## Architecture

```
External Client (WebSocket)
        |
    Gateway (8765)  -- protocol mapping, user routing, session management
        | (internal WebSocket)
    Runtime (8766)  -- session state, agent orchestration, skills, tool loop
        |
    LLM API (Anthropic/MiniMax/Qwen) + Skills Executor + Browser/Search/Memory
```

### Package Layout (`src/openclaw_gateway_runtime/`)

- `protocol.py` -- Frame types and message schema (v1 protocol)
- `transport.py` -- In-memory duplex channel (for testing)
- `ws.py` -- WebSocket adapter
- `gateway/` -- GatewayService, RuntimeManager (multi-user pooling), UserConfig, WS server
- `runtime/` -- RuntimeService (session/run lifecycle), OpenClawAdapter interface + implementations, WS server
- `skills/` -- SkillsRegistry (SKILL.md discovery), SkillsExecutor (script runner), skill_md parser
- `agent_runtime/` -- AnthropicSkillsAdapter, AnthropicClient, SSE streaming, tool clients (browser, DDG search, memory, price compare, Serper batch search)
- `infra/` -- Structured JSON logging, metrics, .env loader

### Adapter Selection (in `RuntimeService.__init__`)

Three `OpenClawAdapter` implementations, selected by priority:
1. Explicit injection (for testing)
2. `LLM_PROVIDER` env var set -> `AnthropicSkillsAdapter`
3. `OPENCLAW_REPO_PATH` env var set -> `CliOpenClawAdapter` (calls OpenClaw Node.js CLI)
4. Default -> `StubEchoAdapter` (deterministic echo, no LLM needed)

### Key Concepts

**Protocol**: JSON text frames with `{"v": 1, "type": str, "session_id": str, "run_id": str, ...}`. Client sends `message.user`, Runtime streams back `stream.chunk`, `event.skill`, `run.started`/`run.end`.

**Cancellation**: `run.cancel` frame for explicit cancel; sending a new `message.user` on the same session auto-cancels the previous run. Uses `CancelToken` (asyncio Event-based).

**Skills**: OpenClaw-style directories with `SKILL.md` (YAML frontmatter + markdown body). Auto-discovered from `skills/` and `SKILLS_PATHS`. Only `enabled: true` skills are registered. Exposed as tools via read_file/exec rather than one tool per skill.

**Telephony**: `call.incoming` -> Gateway routes to per-user RuntimeService instance -> `call.ready` with session_id. RuntimeManager pools up to `MAX_RUNTIME_INSTANCES` instances with idle timeout.

## Testing Conventions

- Tests use `unittest.IsolatedAsyncioTestCase`
- Disable structured logging at module top: `os.environ.setdefault("OPENCLAW_STRUCTURED_LOG", "0")`
- Use `InMemoryDuplex.create_pair()` for Gateway<->Runtime communication in tests
- Inject `StubEchoAdapter()` to avoid LLM dependencies in unit tests

## Configuration

Copy `.env.example` to `.env`. Key variables:

- `LLM_PROVIDER` (required): `anthropic`, `minimax`, or `qwen`
- Provider-specific: `ANTHROPIC_API_KEY`/`MINIMAX_API_KEY`/`QWEN_API_KEY` + model/base_url/timeout
- `SKILLS_PATHS`: Extra skills directories (semicolon-separated on Windows)
- `BROWSER_HEADLESS`, `BROWSER_TIMEOUT_S`: Browser tool config
- `PRICE_COMPARE_SERVICE_URL`: Enables `price_compare` tool when set
- `SERPER_API_KEY`: Enables `batch_web_search` tool when set
- `MEMORY_SERVICE_URL`: External memory REST API; falls back to local JSON
- `LOG_LEVEL`: Default `INFO`; set `DEBUG` for detailed tracing
- `OPENCLAW_STRUCTURED_LOG`: Set to `0` to disable JSON logs (useful for debugging)
- `EXTRA_WORKSPACE_DIRS`: Allow file ops outside repo (pipe-separated absolute paths)

## Language

The README and code comments are primarily in Chinese. Code identifiers and docstrings are in English.
