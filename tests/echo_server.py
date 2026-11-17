"""A tiny real MCP server over stdio, used to test StdioUpstream with an actual subprocess."""
import json
import sys

TOOLS = [
    {"name": "echo", "description": "Echo text", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}},
    {"name": "crash", "description": "Exit the process", "inputSchema": {"type": "object"}},
]

for line in sys.stdin:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    if "id" not in msg:
        continue
    m, rid = msg["method"], msg["id"]
    if m == "initialize":
        out = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "echo", "version": "1"}}
    elif m == "tools/list":
        out = {"tools": TOOLS}
    elif m == "tools/call":
        name = msg["params"]["name"]
        if name == "crash":
            sys.exit(1)
        out = {"content": [{"type": "text", "text": "echo: " + msg["params"]["arguments"].get("text", "")}], "isError": False}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": out}), flush=True)
