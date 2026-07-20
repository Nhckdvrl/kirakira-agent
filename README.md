# Kirakira Agent

Kirakira Agent is a multi-channel AI agent runtime, built by working through the
passive-reply architecture of [`akashic-agent`](https://github.com/kachofugetsu09/akashic-agent)
from a minimal function-calling MVP upward. It has Web, Telegram, QQ/OneBot and
CLI channels; an ordered message bus; session-aware concurrent turns; streaming
OpenAI-compatible tool loops; persistent sessions and long-term memory; plugins,
MCP, skills, scheduling and isolated subagents.

The autonomous proactive/drift chain is intentionally out of scope.

> This is a learning project. The documentation is a first-class part of it: it
> traces how each layer was driven into existence by a concrete problem, which is
> the point of the exercise. Start with
> [docs/VERSION_EVOLUTION.md](docs/VERSION_EVOLUTION.md).

## Quick start

Python 3.11+. The core runtime uses the standard library; the full-screen terminal
client uses Textual and is installed with the project.

```bash
cp .env.example .env          # put your API key here
cp config.example.toml config.toml
python -m kirakira_agent      # streaming full-screen TUI on an interactive terminal
```

```bash
python -m kirakira_agent --tui               # force the full-screen client
python -m kirakira_agent --plain             # streaming line-oriented fallback
python -m kirakira_agent --session research  # create or resume a named local chat
python -m kirakira_agent --serve             # run configured channels
python -m kirakira_agent --workspace /tmp/ws # isolated runtime state
python -m unittest discover -s tests
```

## Layout

```text
kirakira_agent/          the runtime
├── runtime.py           AgentLoop, PassiveTurnPipeline, DefaultReasoner
├── snapshot.py          capability generations + per-turn leases
├── session.py           JSON sessions + SQLite FTS index
├── memory.py            markdown/typed memory + background consolidation
├── retrieval.py         multi-lane recall, RRF fusion, hotness, inject budget
├── context_policy.py    derives context window settings from the model
├── tools/               registry, executor, built-in tools
├── mcp/                 declarative workspace MCP (declarations/host/publisher/watcher)
├── channels/            web, telegram, qq, host
├── plugins.py           plugin loading and programmatic capability declaration
└── ...

_handbook/               contracts: what each subsystem is, its rules, how it fails
docs/                    history and method: why it looks the way it does
skills/                  built-in skills
tests/                   offline regression suite
```

Runtime state (`sessions/`, `memory/`, `uploads/`, `mcp/`, `.kirakira/`) lives
under the workspace root and is gitignored. The workspace resolves as
`--workspace` > `KIRAKIRA_WORKSPACE` > `config.toml [runtime].workspace` > cwd.
Each launch starts a fresh empty local chat unless `--session <name>` is given.
Inside the TUI, `/sessions` opens a keyboard picker for saved chats and
`/session <name>` switches or creates one; `/clear` only clears the view.

## Documentation

`_handbook/` describes **what is true now** — the contract for each subsystem.
It changes in the same commit as the code it documents.

| | |
| --- | --- |
| [workspace-mcp.md](_handbook/workspace-mcp.md) | declaring MCP servers; what happens when a declaration is wrong |
| [snapshot-and-lease.md](_handbook/snapshot-and-lease.md) | why hot reload cannot break an in-flight turn |
| [plugins.md](_handbook/plugins.md) | writing a plugin; declaring capabilities in code |
| [memory.md](_handbook/memory.md) | session vs memory; why recall fuses by rank, not score |

`docs/` describes **how it got here** — history, and the reasoning worth reusing.

| | |
| --- | --- |
| [VERSION_EVOLUTION.md](docs/VERSION_EVOLUTION.md) | MVP → current runtime, one problem at a time |
| [ARCHITECTURE_LESSONS.md](docs/ARCHITECTURE_LESSONS.md) | transferable design judgement, from real decisions here |
| [HANDBOOK_GUIDE.md](docs/HANDBOOK_GUIDE.md) | what a handbook is, why it works, how to write one |
| [DIFFERENCE_AUDIT.md](docs/DIFFERENCE_AUDIT.md) | per-item diff against the reference, and what was deliberately skipped |
| [PROJECT_REPORT.md](docs/PROJECT_REPORT.md) | code walkthrough and data flow |
| [REPLICATION_PLAN.md](docs/REPLICATION_PLAN.md) | scope and completion checklist |

[中文 README](README-cn.md) has the detailed configuration and channel setup.

## License

MIT. See [LICENSE](LICENSE).
