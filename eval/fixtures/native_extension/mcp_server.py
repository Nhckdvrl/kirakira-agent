#!/usr/bin/env python3
"""Real newline JSON-RPC MCP peer; no model responses are fabricated."""
import json
import os
import sys
from urllib.request import urlopen

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": request["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "native-eval", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Read the managed health process and echo a value",
                            "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}}]}
    elif method == "tools/call":
        with urlopen("http://127.0.0.1:" + os.environ["NATIVE_PORT"] + "/health", timeout=5) as response:
            health = json.loads(response.read())
        result = {"content": [{"type": "text", "text": json.dumps({"value": request["params"]["arguments"]["value"], **health})}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
