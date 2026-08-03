<p align="center">
  <img src="./docs/assets/readme/kirakira-agent-icon.png" alt="Kirakira Agent icon" width="96">
</p>

<h1 align="center">Kirakira Agent</h1>

<p align="center">
  <strong>A local-first, continuously running, multi-channel AI Agent Runtime</strong><br>
  <sub>Passive turns · Proactive execution · Drift · Memory · Plugins · MCP</sub>
</p>

<p align="center">
  <a href="./README.md"><b>English</b></a> ·
  <a href="./README-cn.md">简体中文</a> ·
  <a href="./docs/INDEX.md">Documentation</a>
</p>

<p align="center">
  <a href="https://github.com/Nhckdvrl/kirakira-agent/stargazers"><img src="https://img.shields.io/github/stars/Nhckdvrl/kirakira-agent?style=flat&color=f29ab2" alt="GitHub Stars"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/github/license/Nhckdvrl/kirakira-agent?color=738bd7" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/Python-3.11%2B-64a8df" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/runtime-local--first-f2b84b" alt="Local-first runtime">
</p>

<p align="center">
  <img src="./docs/assets/readme/kirakira-agent-hero.png" alt="Kirakira Agent: an empty stage, instruments, starlight, and three execution paths" width="100%">
</p>

## What is Kirakira?

Kirakira is a local-first, multi-channel AI-agent runtime. It brings passive
conversation, proactive execution, long-term memory, and idle background work into
one observable, extensible runtime. Sessions and runtime state stay in the selected
local workspace by default.

## At a glance

| Question | Kirakira's approach |
| --- | --- |
| How does the agent run? | Passive turns, Proactive, and Drift share models, tools, memory, and channels |
| Where does data live? | Sessions, memory, jobs, and traces stay in the selected local workspace by default |
| How is it extended? | Plugins, MCP, skills, tools, and proactive sources |
| How is context governed? | Durable history remains complete; each request gets a budgeted Context Frame projection |
| How is execution inspected? | Tool chains, context traces, token usage, ticks, and module steps are persisted |
| Is it tied to one model? | Main, light, and embedding models use configurable OpenAI-compatible endpoints |

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

Start with the [documentation index](docs/INDEX.md). Current capabilities and
deferrals are in [current status](docs/status/current.md), the main design is in the
[architecture overview](docs/architecture/overview.md), and real-provider evidence
is in the [verification record](docs/operations/verification.md). Task-oriented
guides live under `docs/handbook/`.

[中文说明](README-cn.md)

## License

MIT. See [LICENSE](LICENSE).
