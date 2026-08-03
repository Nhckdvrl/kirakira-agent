# Kirakira Agent

Kirakira is a local, multi-channel AI-agent runtime. Its package boundaries follow
the current Akashic reference architecture while keeping Kirakira's own
implementations and product choices. The optional `Reference/` checkout is audit
input only: production imports, startup, migrations, builds and tests do not read it.

The runtime has three connected execution paths:

- Passive turns: session-aware ReAct loops, tools, MCP, plugins, memory and channels.
- Proactive turns: scheduled alert/content/context collection, decisions, delivery,
  source feedback and durable tick/step traces.
- Drift: user-authored `SKILL.md` background work when a proactive tick has nothing
  to send.

## Start

Python 3.11+ and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv run python main.py setup    # interactive setup
uv run python main.py init     # non-interactive workspace initialization
uv run python main.py          # supervisor -> gateway
uv run python main.py gateway  # unmanaged debug gateway
```

Without `config.toml`, the default command opens setup. The public compatibility
entry remains available:

```bash
python -m kirakira_agent --tui
python -m kirakira_agent --plain
python -m kirakira_agent --session research
```

Minimal model configuration:

```toml
[llm.main]
model = "deepseek-v4-flash"
base_url = "https://api.deepseek.com/v1"
api_key = "${DEEPSEEK_API_KEY}"
context_window = 128000 # use the capacity advertised by your provider

[agent.context]
effective_context_percent = 0.9
```

Embeddings are configured separately under `[memory.embedding]`; a chat-completion
endpoint is not assumed to provide embeddings.

## Current runtime contracts

- SQLite is the authoritative session/message store. Normal saves are append-only;
  prompt pressure never deletes or rewrites durable history.
- Every model attempt renders a bounded projection. It can drop optional prompt
  sections and progressively reduce projected history, while storing the selected
  plan, approximate input estimate and exact/partial/unavailable provider usage.
- Shell execution uses one lifecycle for foreground/background processes, PTY input,
  output polling, cancellation and process-group cleanup. Child agents have isolated
  execution ownership and bounded admission.
- Schedules support one-shot, duration, interval and 5/6-field cron triggers with
  IANA time zones, instant/soft tiers and isolated soft-turn sessions.
- Tools carry risk, discovery and source metadata. MCP/plugin tools can remain
  deferred until `tool_search` unlocks them.
- Workspace schema changes run through a locked Yoyo migration ledger. Migration
  files are append-only.
- Akasha v1 is a supported selectable memory engine. Akasha v2 is intentionally not
  required for the current milestone.

The repository includes a minimal semantic change-impact gate and a secret-safe
online verifier:

```bash
uv run pytest -q
uv run kirakira-impact --base HEAD --run
uv run kirakira-verify-online
```

The online verifier uses an isolated temporary workspace and checks configured model
text, forced tool calls, token usage, embeddings, runtime tool execution, context
governance and Akasha v1 ingest/retrieval. It never prints credentials.

## Layout

```text
agent/            reasoning, lifecycle, tools, MCP, plugins, scheduling, subagents
bootstrap/        composition root, setup, supervisor, control and dashboard wiring
bus/              message queue and logical event contracts
core/             shared schema, network and memory contracts/runtime
infra/            provider, channel, control and persistence adapters
session/          authoritative session and message-embedding stores
memory2/          default structured-memory algorithms and storage
plugins/          first-party memory, proactive and Drift implementations
plugin_packages/  distributable plugins
proactive_v2/     proactive kernel, frame and tick orchestration
frontend/         terminal and local Web presentation surfaces
eval/             memory evaluation harnesses
migrations/       append-only workspace migrations
scripts/          migration, change-impact and online verification tools
kirakira_agent/   public `python -m kirakira_agent` entry shell only
```

Runtime state lives under the selected workspace. Resolution order is
`--workspace` → `KIRAKIRA_WORKSPACE` → `[runtime].workspace` → current directory.

## Documentation

Start with [docs/INDEX.md](docs/INDEX.md). Current gaps and deliberate deferrals are
in [docs/NOW.md](docs/NOW.md) and the complete capability audit is in
[docs/DIFFERENCE_AUDIT.md](docs/DIFFERENCE_AUDIT.md). Operational contracts live in
[_handbook/](_handbook/); verified real-provider evidence lives in
[docs/design/live-verification.md](docs/design/live-verification.md).

[中文说明](README-cn.md)

## License

MIT. See [LICENSE](LICENSE).
