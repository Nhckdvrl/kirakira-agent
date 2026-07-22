# Kirakira Agent

Kirakira Agent is a multi-channel AI agent runtime built after
[`akashic-agent`](https://github.com/kachofugetsu09/akashic-agent) from a minimal
function-calling MVP upward. It is not just a "you ask, it answers" bot — it has
**three parallel chains**, and the latter two are what set it apart from an ordinary
chatbot:

- **Passive reply** — Web, Telegram, QQ/OneBot and CLI channels; an ordered message
  bus; session-aware concurrent turns; streaming OpenAI-compatible tool loops;
  persistent sessions and long-term memory; plugins, MCP, skills, scheduling and
  isolated subagents.
- **Proactive push** — a background loop that adaptively paces its polling with an
  energy model, pulls three channels of data (`alert`/`content`/`context`) and lets
  the LLM decide whether to reach out. See [_handbook/proactive.md](_handbook/proactive.md).
- **Drift** — when the proactive chain has nothing to push, it reuses the same agent
  loop to run a background task defined by a user-authored `SKILL.md`, with
  cross-run continuity. See [_handbook/drift.md](_handbook/drift.md).

Proactive/Drift are MVP-level: the differentiating essence is implemented and wired
in; the reference's heavier machinery (phase-graph kernel, snapshot hot reload,
semantic-interest vectors) is deliberately deferred. Both are off by default and
enabled under `[proactive]` in `config.toml`.

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
python -m kirakira_agent --proactive         # run one proactive tick now, print status, exit
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
├── prompting/           named prompt blocks, context frames, trim plans
├── tools/               registry, executor, built-in tools
├── mcp/                 declarative workspace MCP (declarations/host/publisher/watcher)
├── channels/            web, telegram, qq, host
├── proactive/           energy model, three channels, pluggable sources, judge, tick loop
├── drift/               idle-task chain: skill discovery, run state, runner (reuses agent loop)
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
During a turn the clients show the selected context plan, estimated/input-budget
tokens and history size. Named prompt sections are re-rendered under pressure;
the complete retry/section/cache/model-usage trace is stored with the assistant
session message, while `metadata.context_budget` stores the next-turn baseline.

For DeepSeek, set its advertised capacity explicitly so the runtime and provider
share one budget:

```toml
[llm.main]
model = "deepseek-v4-flash"
base_url = "https://api.deepseek.com/v1"
api_key = "${DEEPSEEK_API_KEY}"
context_window = 128000  # set this to the capacity documented by your provider

[agent.context]
effective_context_percent = 0.9
```

## Documentation

`_handbook/` describes **what is true now** — the contract for each subsystem.
It changes in the same commit as the code it documents.

| | |
| --- | --- |
| [workspace-mcp.md](_handbook/workspace-mcp.md) | declaring MCP servers; what happens when a declaration is wrong |
| [snapshot-and-lease.md](_handbook/snapshot-and-lease.md) | why hot reload cannot break an in-flight turn |
| [plugins.md](_handbook/plugins.md) | writing a plugin; declaring capabilities in code |
| [memory.md](_handbook/memory.md) | session vs memory; why recall fuses by rank, not score |
| [context-management.md](_handbook/context-management.md) | prompt blocks, token budget, semantic retries and traces |
| [cli-and-sessions.md](_handbook/cli-and-sessions.md) | TUI ownership, streaming finality and saved-session behavior |
| [proactive.md](_handbook/proactive.md) | the energy model, three channels, pluggable sources and the push decision |
| [drift.md](_handbook/drift.md) | idle-task skills, one drift run = one agent run, cross-run continuity |

`docs/` describes **how it got here** — history, and the reasoning worth reusing.

| | |
| --- | --- |
| [VERSION_EVOLUTION.md](docs/VERSION_EVOLUTION.md) | MVP → current runtime, one problem at a time |
| [DIFFERENCE_AUDIT.md](docs/DIFFERENCE_AUDIT.md) | per-item diff against the reference, scope, and what is MVP / deferred |
| [RESUME_INTERVIEW_GUIDE.md](docs/RESUME_INTERVIEW_GUIDE.md) | résumé wording and interview Q&A, incl. the three-chain differentiator |

[中文 README](README-cn.md) has the detailed configuration and channel setup.

## License

MIT. See [LICENSE](LICENSE).
