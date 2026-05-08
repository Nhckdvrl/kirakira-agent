# kirakira-agent

手搓一个 agent harness，记录学习过程。

Kirakira Agent 是一个小型 Python coding agent harness，核心思路来自
`learn-claude-code`：模型负责决策，harness 提供工具、上下文、观察和执行边界。

## Quick Start

```bash
python3 -m kirakira_agent
```

需要在 `.env` 中配置 OpenAI-compatible 模型服务：

```bash
OPENAI_COMPATIBLE_BASE_URL=http://localhost:8000/v1
OPENAI_COMPATIBLE_API_KEY=not-needed-for-local
MODEL_ID=qwen2.5-coder
```

DeepSeek 示例：

```bash
OPENAI_COMPATIBLE_BASE_URL=https://api.deepseek.com
OPENAI_COMPATIBLE_API_KEY=your-deepseek-api-key
MODEL_ID=deepseek-v4-flash
```

DeepSeek V4 默认会关闭 thinking mode，以避免工具调用多轮对话时必须回传
`reasoning_content` 的协议限制。如需显式开启：

```bash
OPENAI_COMPATIBLE_THINKING=enabled
```

兼容 DeepSeek、Qwen、GLM、vLLM、LM Studio 等支持 `/v1/chat/completions`
和 tool calling 的服务。

## CLI Commands

- `/tools`：列出工具。
- `/skills`：列出 `skills/**/SKILL.md`。
- `/compact`：压缩当前上下文。
- `/exit`：退出。

## Built-in Tools

- `bash`：在 workspace 内执行命令，带危险命令拦截和超时。
- `read_file`：读取 workspace 内文件。
- `write_file`：写入 workspace 内文件。
- `edit_file`：精确替换文件内容。
- `load_skill`：按名称加载 skill。
- `compact`：触发上下文压缩。

## Development

```bash
python3 -m unittest
```
