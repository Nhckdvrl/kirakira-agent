# Third-Party Notices

This project vendors source code from other open-source projects. Their
copyright notices and licenses are reproduced below as required.

## akashic-agent

The following directories are vendored (copied, with import paths adapted) from
the **akashic-agent** project and remain under its original MIT License:

- `kirakira_agent/memory2/`  — Memory v2 subsystem (structured SQLite store,
  memorizer, retriever, embedder, post-response worker, etc.)
- `kirakira_agent/coremem/`  — core memory engine / markdown store

Source: https://github.com/kachofugetsu09/akashic-agent

Adaptations made when vendoring: internal import paths were rewritten to the
`kirakira_agent.*` namespace, and external dependencies were routed through the
compatibility shims in `kirakira_agent/_compat/`. The original logic is
otherwise preserved.

```
MIT License

Copyright (c) 2026 kachofugetsu09

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Note on `web_search` / `web_fetch` / `vision` / `tool_search`

The `web_search` (Exa MCP endpoint), `web_fetch` (format modes), `vision`
(Pillow encoding) and `tool_search` (CJK-aware scoring) tool behaviours in
`kirakira_agent/tools/builtins.py` were re-implemented following akashic-agent's
approach but written against kirakira's own tool interfaces.
