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

Kirakira is a personal AI companion and assistant for everyday conversation,
interests, learning, life and work. Talk something through, share a small discovery,
or ask it to get something done. With proactive interaction enabled, it can also
investigate incoming information or pick up an invited conversation after an idle wait.

The focus is continuity: a familiar way of interacting, memory it can return to,
and practical help that carries across conversations. Personality, memory and
proactive behavior work within the same runtime as tools, tasks and delivery.

## The experience

**Make it your kind of companion.** Combine warmth, candor, humor, curiosity,
answer depth and emoji use independently. Choose whether it should listen first
or offer a small next step, and whether casual chat should include suggestions.
An editable character foundation and optional voice examples add room for a personal
voice. Explicit requests and task formats take priority.
[Personality and interaction →](docs/handbook/personality.md)

**Remember, then check.** Akasha learns from completed conversations, combining
semantic recall with episodic associations. When a recalled snippet is insufficient,
the agent can locate and read the original text, including corrections and conditions.
Markdown memory holds the long-term profile; source messages remain intact.
[How memory works →](docs/architecture/memory.md)

**Reach out for a reason.** Two paths share the same execution and delivery chain:
configured sources can trigger investigation, and optional idle planning can propose
a conversation, practical help or research. Both can choose to skip. Idle conversation
has its own opt-in; budgets, deduplication, pause and topic completion bound initiative.
[Memory and proactive setup →](docs/handbook/memory-proactive.md)

**Follow through on real tasks.** Streaming conversation, files, Shell, MCP, child
agents and background tasks use the same task lifecycle. Tool results, interruption,
cancellation, recovery and delivery receipts stay connected to their original execution.
Models, channels and tools are replaceable plugins; chat and embedding models are
configured independently.
[Runtime architecture →](docs/architecture/overview.md)

For example, an ongoing interest can become a conversation, a remembered constraint,
and later a useful response to a new event. A configured feed, webpage or mailbox
provides the event; the agent checks the relevant history before deciding to share
or stay quiet. You can inspect the sources and delivery, change the rule, or pause it.
[Follow-up workflows →](docs/handbook/followups.md)

## Get started

Use Python **3.14.6**, [uv](https://docs.astral.sh/uv/), and **Node.js 24** for the
Web workspace. All Python commands use one project `.venv` and the checked-in lockfile.
Choose a new, empty workspace on a local disk:

```bash
git clone https://github.com/Nhckdvrl/kirakira-agent.git
cd kirakira-agent
uv sync --frozen --extra providers --extra memory
npm ci
npm run build

uv run --frozen --no-sync kirakira-agent init --workspace "$HOME/.kirakira/workspace" --profile web
uv run --frozen --no-sync kirakira-agent gateway --config "$HOME/.kirakira/workspace/config.toml" --workspace "$HOME/.kirakira/workspace"
```

Open **http://localhost:2236**. On first launch, the owner account is `kirakira`;
the initial password is stored privately in `workspace/web-auth/initial-password`.
Set your own password, connect a model, and start a conversation. The gateway runs
in the foreground; keep it running while using the agent.

Then shape its **Personality and interaction** settings, enable
[local memory](docs/handbook/memory-proactive.md#本地记忆), or set up
[follow-ups](docs/handbook/followups.md). Idle initiative is off by default.
For terminal model configuration, headless use, Telegram and upgrades, follow the
[getting started guide](docs/handbook/start.md).

“Local-first” means messages and execution state live in your workspace.
A configured remote model or external tool still receives the inputs needed for
its request. The embedding encoder runs locally on CPU after an explicit model download.

## What we have measured

| Question | Observed result | Conditions and records |
| --- | --- | --- |
| Does following a memory clue back to the source help? | Evidence-supported answers **0/8 → 5/8** on difficult middle/tail questions | Same initial material; source access adds evidence and model calls. [Memory scenarios](docs/operations/evaluations/2026-10-22-memory-scenarios.md) |
| Can past personal conditions affect a new event decision? | Relevant history presented **12/12**; correct share/skip decision **11/12** | Controlled mailbox scenarios, including the failed decision. [Follow-up experiment](docs/operations/evaluations/2026-10-21-mail-followups.md) |
| Can it pick up everyday conversations and stop? | **32/32 assertions** across four invited topics | Actual idle waits; source, delivery, duplicate and stopping checks. [Companion experiment](docs/operations/evaluations/2026-10-22-companion-personality.md) |
| Does the execution chain work with a real model? | **13/13 runtime scenarios** | Files, Shell, MCP, child tasks, interruption and recovery. [Verification records](docs/operations/verification.md) |

These are scoped experiments, not a claim of overall memory superiority or user
satisfaction. Personality controls are implemented and tested, while replies can
still add unwanted advice or unsupported details. Cross-model consistency,
long-term companionship and useful-versus-disruptive initiative need further study.
The [reports](docs/operations/verification.md) retain failures, costs and version details;
[design direction](docs/architecture/design-direction.md) explains the choices.

## Development and verification

```bash
uv run --frozen --no-sync pytest -q
npm run typecheck
uv run --frozen --no-sync python -m scripts.check_docs
```

The [evaluation guide](eval/README.md) provides isolated real-model tests and
reproducible memory experiments. The [maintenance guide](scripts/README.md) covers
source checks and generated-file cleanup.

```text
agent/          composition, lifecycle, configuration and supervisor
bootstrap/      host resources and startup
plugins/        conversation, personality, memory, tools, initiative and channels
frontend/       Web interface and theme
kirakira_agent/ public CLI and workspace setup
sdk/            programmatic client
eval/           functional verification and memory experiments
tests/          runtime and integration contracts
scripts/        build, migration and maintenance
docs/           guides, architecture and evaluation evidence
```

[Documentation](docs/INDEX.md) · [Getting started](docs/handbook/start.md) ·
[Personality](docs/handbook/personality.md) · [Memory and initiative](docs/handbook/memory-proactive.md) ·
[Architecture](docs/architecture/overview.md) · [SDK](sdk/python/README.md) ·
[Current state](docs/status/current.md) · [Roadmap](docs/status/roadmap.md)

## License

MIT. See [LICENSE](LICENSE).
