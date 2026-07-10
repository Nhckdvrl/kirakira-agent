# Kirakira Agent

Kirakira Agent is a multi-channel AI agent runtime modeled after the passive
reply architecture of `akashic-agent`. It includes Web, Telegram, QQ/OneBot,
and CLI channels; an ordered message bus; concurrent session-aware turns;
streaming OpenAI-compatible tool loops; persistent sessions and memory;
plugins, MCP, skills, scheduling, and isolated subagents.

The autonomous proactive/drift chain is intentionally out of scope. See the
[detailed Chinese README](README-cn.md) and
[project report](docs/PROJECT_REPORT.md) for architecture, configuration,
verification results, and the exact Reference comparison.

## Quick Start

Python 3.11 or newer is required.

```bash
cp config.example.toml config.toml
export DEEPSEEK_API_KEY=your-key
python -m kirakira_agent
```

Run all configured passive channels:

```bash
python -m kirakira_agent --serve
```

Run the tests:

```bash
python -m unittest discover -s tests -v
```

Runtime state is stored in `sessions/`, `memory/`, `uploads/`, and
`.kirakira/`; `Reference/` and local secrets are ignored by Git.
