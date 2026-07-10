# kirakira-agent

A Python coding agent harness, built as a hands-on learning project.

Status: experimental and intentionally small.

Kirakira Agent follows the harness engineering idea from `learn-claude-code`:
the model decides, while the harness provides tools, context, observations, and
execution boundaries.

Chinese README: [README-cn.md](README-cn.md)

## Quick Start

```bash
python3 -m kirakira_agent
```

Configure an OpenAI-compatible model service in `.env`:

```bash
OPENAI_COMPATIBLE_BASE_URL=http://localhost:8000/v1
OPENAI_COMPATIBLE_API_KEY=not-needed-for-local
MODEL_ID=qwen2.5-coder
```

DeepSeek example:

```bash
OPENAI_COMPATIBLE_BASE_URL=https://api.deepseek.com
OPENAI_COMPATIBLE_API_KEY=your-deepseek-api-key
MODEL_ID=deepseek-v4-flash
```

DeepSeek V4 thinking mode is disabled by default to avoid the multi-turn tool
calling requirement to pass `reasoning_content` back to the API. To enable it:

```bash
OPENAI_COMPATIBLE_THINKING=enabled
```

It works with services that expose `/v1/chat/completions` and support tool
calling, such as DeepSeek, Qwen, GLM, vLLM, and LM Studio compatible servers.

## CLI Commands

- `/tools`: List available tools.
- `/skills`: List loadable `skills/**/SKILL.md` files.
- `/compact`: Compress the current conversation context.
- `/exit`: Exit the REPL.

## Passive Channels

The passive runtime can also serve Web, Telegram, and QQ/OneBot channels.

Web:

```bash
python3 -m kirakira_agent --serve --web
# open http://127.0.0.1:8765
```

Telegram:

```bash
TELEGRAM_BOT_TOKEN=123456:abcdef python3 -m kirakira_agent --serve --telegram
```

Optional Telegram allow list:

```bash
TELEGRAM_ALLOW_FROM=123456789,alice
```

QQ via NapCat/OneBot HTTP:

```bash
python3 -m kirakira_agent --serve --qq
```

Configure NapCat/OneBot to POST events to:

```text
http://127.0.0.1:8766/qq/webhook
```

Common QQ environment variables:

```bash
QQ_BOT_UIN=12345
ONEBOT_API_BASE_URL=http://127.0.0.1:3000
QQ_GROUP_ALLOW=777,888
QQ_REQUIRE_AT=true
```

## Built-in Tools

- `bash`: Run shell commands in the workspace, with basic dangerous-command
  blocking and timeouts.
- `read_file`: Read files inside the workspace.
- `write_file`: Write files inside the workspace.
- `edit_file`: Replace exact text in a workspace file.
- `load_skill`: Load a skill by name.
- `compact`: Trigger context compression.

## Development

```bash
python3 -m unittest discover -v
```
