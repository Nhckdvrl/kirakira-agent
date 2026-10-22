<p align="center">
  <img src="./docs/assets/readme/kirakira-agent-icon.png" alt="Kirakira Agent icon" width="96">
</p>

<h1 align="center">Kirakira Agent</h1>

<p align="center">
  <strong>A personal AI companion with lasting memory and a personality you can shape</strong><br>
  <sub>Everyday companionship · Practical help · Proactive interaction · Local-first</sub>
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

Kirakira is a personal AI companion and assistant for everyday conversations,
interests, learning, life and work. Chat, think something through together, or
ask it to do a task. With proactive interaction enabled, it can investigate new
events or revisit a conversation while idle, then choose to help, reach out or stay quiet.

Shape how you interact: warm can also be candid, reserved can still be playful,
and listening or offering advice are separate choices. Conversation, task replies
and authorized proactive messages share those settings, with the current request
taking priority. Memory keeps its original sources; tools, task state and delivery
results belong to the same recoverable runtime.

Tested experiences include continuing invited conversations about painting,
reading, guitar and gardening after a real idle wait, then stopping when the topic
ends. Other experiments recover corrected plans or personal constraints before
acting on a new event. Their [records and results](docs/operations/verification.md)
are kept separately.

## Experiences we focus on

| Experience | How it works | Evidence |
| --- | --- | --- |
| Make it your kind of companion | Combine a character foundation, six expression dimensions, support and suggestion preferences, and optional voice examples | [Expression, behavior and real-task experiments](docs/operations/evaluations/2026-10-22-companion-personality.md) |
| Remember, then check the details | Follow a recalled source back to its original text and use confirmed or corrected history in current answers and actions | [Evidence-supported answers on eight difficult long-source questions: 0/8 → 5/8](docs/operations/evaluations/2026-10-22-memory-scenarios.md) |
| Reach out, with a way to stop | Event investigation and idle initiative can share or skip, with separate controls, budgets, deduplication and pause | [Four everyday topics, real idle waits and stopping checks](docs/operations/evaluations/2026-10-22-companion-personality.md) |
| Follow through on practical help | Use files, Shell, MCP and background tasks, check actual results and delivery, and support interruption, cancellation and recovery | [Real-model runtime and recovery records](docs/operations/verification.md) |

Results are scoped to their scenarios and implementation versions. The long-source
comparison allows additional evidence and model calls; behavior checks and long-term
user experience are evaluated separately. See the [product design](docs/architecture/design-direction.md)
and [personality design](docs/architecture/personality.md) for choices and references.

## Built for ongoing companionship and work

**Personality and interaction.** Keep a stable character foundation while adjusting
expression and response habits independently. Temporary requests do not claim to save
permanent preferences, and voice examples do not become shared history.
See [personality settings](docs/handbook/personality.md).

**Conversation and tools.** Streaming replies, ReAct tool execution, file operations,
Shell and terminal sessions, managed processes, MCP, and child agents work within
the same task lifecycle. Sessions support interruption, cancellation, and recovery.

**Memory with evidence.** Akasha learns from completed conversation segments.
Embeddings and graph state are derived from the message history and can be rebuilt;
retrieved evidence remains traceable to its source. The local embedding provider
runs on CPU after an explicit model download.

**Proactive interaction.** Content, Alert, and Context events feed the agent's
interest and wake decisions. Investigation, sharing, duplicate handling, and delivery
confirmation are part of the execution flow. Optional idle planning can continue a
conversation, help with an unfinished matter or research a discovery. Local integrations
can submit events through authenticated RPC.

**A runtime you can extend.** Models, tools, memory, channels, and the Web workspace
are plugins. Configuration and state belong to each plugin, and selected plugins
can be updated within the running composition. Chat and embedding models are independently
configurable; messages and execution state stay in the local workspace.

## Start

Use Python 3.14.6, [uv](https://docs.astral.sh/uv/), and Node.js/npm for the Web
workspace. Python commands use the single project `.venv` and the pinned `uv.lock`.
Run these commands from the repository root:

```bash
uv sync --frozen --group dev --group eval --extra providers
npm ci
npm run build

uv run kirakira-agent init --workspace /absolute/path/to/workspace --profile web
uv run kirakira-agent gateway --config /absolute/path/to/workspace/config.toml --workspace /absolute/path/to/workspace
```

Replace the example paths with your own. Keep the workspace on a local disk and
configure your preferred provider in the Web workspace. Model credentials stay private.
For a headless workspace, choose `--profile headless` and skip the frontend build.

To enable local memory or configure a proactive target, follow the
[memory and proactive setup guide](docs/handbook/memory-proactive.md).
Existing workspaces have separate [upgrade](docs/handbook/memory-proactive.md) and
[migration](docs/handbook/start.md) instructions.

## Verification

```bash
uv run pytest -q
uv run kirakira-verify-online --model-config /private/path/model.json --key-file /private/path/model.key --workspace /tmp/new-eval --output /tmp/native-eval.json
```

The online verifier calls the configured model provider in an isolated workspace. It exercises
messages, files, Shell, MCP, managed processes, child agents, interruption, restart,
and plugin updates. Memory and proactive experiments, including their measured
results and conditions, are documented in the [verification record](docs/operations/verification.md).

## Layout

```text
agent/            composition, lifecycle, configuration, and supervisor
bootstrap/        host resources and application startup
plugins/          conversation, models, tools, memory, proactive interaction, and channels
frontend/         shared Web interface and theme
kirakira_agent/   public CLI, workspace setup, and distribution integration
sdk/              programmatic client
eval/             functional verification and memory experiments
tests/            runtime and integration contracts
scripts/          build, migration, and verification tools
docs/             architecture, guides, and evaluation records
```

## Documentation

[Documentation index](docs/INDEX.md) ·
[Getting started](docs/handbook/start.md) ·
[Memory and proactive interaction](docs/handbook/memory-proactive.md) ·
[Personality and interaction](docs/handbook/personality.md) ·
[Architecture](docs/architecture/overview.md) ·
[Verification](docs/operations/verification.md) ·
[SDK](sdk/python/README.md)

## License

MIT. See [LICENSE](LICENSE).
