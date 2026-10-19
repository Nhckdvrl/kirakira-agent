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
  <img src="https://img.shields.io/badge/Python-3.14.6-64a8df" alt="Python 3.14.6">
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

Its optional episodic memory keeps original conversations as the source of truth.
Exact semantic recall and associative graph completion recover related experiences
with dates and source references; the agent can expand those references to inspect
the original exchange. Learning happens after a committed conversation, while
explicit recall leaves the learned graph unchanged. Checkpoints support recovery
and replay from verified source messages and fixed embeddings.

Plugin services use generation-scoped leases. Model recovery preserves completed
tool results, while proactive decisions and Drift proposals keep durable state.
Akasha2 uses pinned upstream algorithms with Kirakira's source-consumption and
atomic checkpoint adapter; v1 remains available. See the
[architecture](./docs/architecture/overview.md), [current scope](./docs/status/current.md),
and [verification record](./docs/operations/verification.md).

## At a glance

| Question | Kirakira's approach |
| --- | --- |
| How does the agent run? | Passive turns, Proactive, and Drift share models, tools, memory, and channels |
| Where does data live? | Sessions, memory, jobs, and traces stay in the selected local workspace by default |
| Can sessions work in different repos? | Projects set a one-time default; each session has its own cwd, live repository rules, file tools, Shell, and child-task inheritance |
| How does it remember? | Exact recall plus associative completion, with original-message references and replayable graph state |
| How is it extended? | Plugins, MCP, skills, tools, and proactive sources |
| How is context governed? | Durable history stays complete; old units roll into a task summary while the newest 20k tokens stay raw |
| How is execution inspected? | Tool chains, context traces, token usage, ticks, and module steps are persisted |
| Is it tied to one model? | Main, light, and embedding models use configurable OpenAI-compatible endpoints |

The runtime has three connected execution paths:

- Passive turns: session-aware ReAct loops, tools, MCP, plugins, memory and channels.
- Proactive turns: scheduled alert/content/context collection, decisions, delivery,
  source feedback and durable tick/step traces.
- Drift: user-authored `SKILL.md` background work when a proactive tick has nothing
  to send.

## Start

Use Python 3.14.6 and [uv](https://docs.astral.sh/uv/). The version is pinned in
`.python-version`; local development and CI use the same interpreter and `uv.lock`.
Run all project commands through the single project `.venv`:

```bash
uv sync --frozen --group dev
uv run python main.py setup    # interactive setup
uv run python main.py init     # non-interactive workspace initialization
uv run python main.py          # supervisor -> gateway
uv run python main.py gateway  # unmanaged debug gateway
```

Without `config.toml`, the default command opens setup. The public compatibility
entry remains available:

```bash
uv run python -m kirakira_agent --tui
uv run python -m kirakira_agent --plain
uv run python -m kirakira_agent --session research
```

Minimal model configuration:

```toml
[llm.main]
model = "deepseek-v4-flash"
base_url = "https://api.deepseek.com/v1"
api_key = "${DEEPSEEK_API_KEY}"
context_window = 128000 # use the capacity advertised by your provider

```

Embeddings are configured separately under `[memory.embedding]`; a chat-completion
endpoint is not assumed to provide embeddings.

## Current runtime contracts

- SQLite is the authoritative session/message store. Normal saves are append-only;
  prompt pressure never deletes or rewrites durable history.
- Every model call passes the context compaction gate. At 74% of the model window,
  older complete interactions become a rolling structured summary while the newest
  20k tokens remain raw; the hard edge reserves `max_tokens` for output.
- Shell execution uses one lifecycle for foreground/background processes, PTY input,
  output polling, cancellation and process-group cleanup. Child agents have isolated
  execution ownership and bounded admission.
- Schedules support one-shot, duration, interval and 5/6-field cron triggers with
  IANA time zones, source-session notify/run actions, explicit history scope, durable
  fire identities and confirmed local delivery receipts.
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

The composition kernel is byte-identical to the pinned Akashic revision.
See [runtime architecture](docs/architecture/overview.md) for ownership and integration boundaries.

```text
agent/            native Context/Fiber/Effect kernel, configuration and supervisor
bootstrap/        distribution, application facade and CLI/setup
plugins/ledger/   canonical Session/messages, immutable inputs and owner transactions
plugins/          ordinary reply, source, ReAct, model, tool, MCP, memory and proactive owners
bus/              message queue and event contracts
core/             shared schema, network and memory contracts
infra/            provider, channel, control and persistence adapters
memory2/          default structured-memory algorithms and storage
plugin_packages/  distributable extension packages
proactive_v2/     proactive frame and tick orchestration
frontend/         terminal and local Web presentation surfaces
eval/             memory evaluation harnesses
migrations/       append-only workspace migrations
scripts/          verification, migration and change-impact tools
kirakira_agent/   public `python -m kirakira_agent` entry shell
```

Runtime state lives under the selected workspace. Resolution order is
`--workspace` → `KIRAKIRA_WORKSPACE` → `[runtime].workspace` → current directory.

## Documentation

Start with the [documentation index](docs/INDEX.md). Current capabilities and
deferrals are in [current status](docs/status/current.md), the main design is in the
[architecture overview](docs/architecture/overview.md), and real-provider evidence
is in the [verification record](docs/operations/verification.md). The
[memory experiments](docs/operations/evaluations/2026-10-19-memory-followup.md) include
public QA controls, held-out questions, source integrity and measured recovery
costs. Task guides live under `docs/handbook/`.

[中文说明](README-cn.md)

## License

MIT. See [LICENSE](LICENSE).

## Attribution

Kirakira learns from [Akashic Agent](https://github.com/kachofugetsu09/akashic-agent).
The optional Akasha2 algorithms are adapted from commit `d85b00c`; the vendored
[MIT license](./plugins/akasha2/LICENSE) and [file provenance](./plugins/akasha2/UPSTREAM.json)
ship with the package. The runtime adapter and evaluation results are maintained here.
Project and working-directory behavior also follows the current upstream design;
its source contracts and local implementation boundary are recorded in
[ADR0015](./docs/decisions/0015-projects-and-session-directories.md).
