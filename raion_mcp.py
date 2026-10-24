"""Raion's own tools for the claude CLI: a minimal MCP server over stdio. It only relays: the tool list and every call
go to the agent's local tool API (wa_agent.tool_api), which knows whose chat this key belongs to and does the work."""
import json, os, sys, urllib.request

URL, KEY = os.environ["RAION_URL"], os.environ["RAION_KEY"]
sys.stdin.reconfigure(encoding="utf-8")  # Hindi text; replies stay ASCII-escaped JSON


def app(path, body=None):
    req = urllib.request.Request(URL + path, None if body is None else json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "X-Raion-Key": KEY})
    with urllib.request.urlopen(req, timeout=1800) as r:  # no real limit: Office conversions, big downloads
        return json.loads(r.read())


for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:  # notifications need no answer
        continue
    m = msg.get("method")
    try:
        if m == "initialize":
            res = {"protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                   "capabilities": {"tools": {}}, "serverInfo": {"name": "raion", "version": "1"}}
        elif m == "tools/list":
            res = {"tools": app("/tools")}
        elif m == "tools/call":
            res = app("/call", msg["params"])
        else:  # ping and anything else
            res = {}
        out = {"jsonrpc": "2.0", "id": msg["id"], "result": res}
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        out = {"jsonrpc": "2.0", "id": msg["id"], **(
            {"result": {"isError": True, "content": [{"type": "text", "text": err}]}} if m == "tools/call"
            else {"error": {"code": -32603, "message": err}})}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
