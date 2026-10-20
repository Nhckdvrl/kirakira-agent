<p align="center">
  <img src="./docs/assets/readme/kirakira-agent-icon.png" alt="Kirakira Agent icon" width="96">
</p>

<h1 align="center">Kirakira Agent</h1>

<p align="center">
  <strong>A local-first, continuously running, multi-channel AI Agent Runtime</strong><br>
  <sub>Passive turns · Proactive execution · Memory · Plugins · MCP</sub>
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

Kirakira connects past decisions with ongoing work. It can recover a project’s
updated plan and constraints from earlier conversations, investigate an incoming
event, and send a reminder whose history and delivery can be traced.
Conversation, memory, tools and proactive tasks run in one local-first runtime.

Its episodic memory keeps original conversations as the source of truth. Semantic
recall and associative graph completion recover related experiences across sessions,
with references the agent can follow back to the original exchange. Markdown memory
holds explicit notes and preferences alongside those experiences.

Proactive interaction starts with events and context: the agent can investigate new
information, decide whether it warrants a message, and deliver it to a configured
session. Conversations and proactive tasks share models, tools, and memory.

A tested workflow: confirm a release plan, correct it in another session, then
receive a release-check event that contains none of those decisions. The agent
recovers the updated schedule, owner and data restrictions before sending a reminder.
See the [memory design](docs/architecture/memory.md) and
[verification record](docs/operations/verification.md) for evidence and limits.

## At a glance

| Question | Kirakira's approach |
| --- | --- |
| How does the agent run? | Conversation and proactive tasks share a persistent runtime |
| Where does data live? | Messages, memory, configuration, and execution state stay in the selected local workspace |
| Can sessions work in different repos? | Projects and session working directories carry repository context into file tools, Shell, and child agents |
| How does it remember? | Semantic recall, associative completion, and Markdown notes, with original-message evidence |
| How is it extended? | Plugins, MCP servers, skills, tools, and event sources |
| How is context managed? | Context projection and compaction keep requests within budget while preserving stored messages |
| How is execution inspected? | Model usage, tool results, task state, and delivery receipts are recorded |
| Is it tied to one model? | Chat and embedding providers are independently configurable plugins |

## Built for ongoing work

**Conversation and tools.** Streaming replies, ReAct tool execution, file operations,
Shell and terminal sessions, managed processes, MCP, and child agents work within
the same task lifecycle. Sessions support interruption, cancellation, and recovery.

**Memory with evidence.** Akasha learns from completed conversation segments.
Embeddings and graph state are derived from the message history and can be rebuilt;
retrieved evidence remains traceable to its source. The local embedding provider
runs on CPU after an explicit model download.

**Proactive interaction.** Content, Alert, and Context events feed the agent's
interest and wake decisions. Investigation, sharing, duplicate handling, and delivery
confirmation are part of the execution flow. Local integrations can submit events
through authenticated RPC.

**A runtime you can extend.** Models, tools, memory, channels, and the Web workspace
are plugins. Configuration and state belong to each plugin, and selected plugins
can be updated within the running composition.

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
[Architecture](docs/architecture/overview.md) ·
[Verification](docs/operations/verification.md) ·
[SDK](sdk/python/README.md)

## License

MIT. See [LICENSE](LICENSE).
