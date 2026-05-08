# kirakira-agent

A Python coding agent harness, built as a hands-on learning project.

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
python3 -m unittest
```
