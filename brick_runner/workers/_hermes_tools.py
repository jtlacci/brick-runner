"""The fixture tools for a Hermes evaluation: an MCP server (stdio) inside the sandbox.

Hermes starts it (mcp_servers.chip in the runner's config). Its tools are the
brick evaluation's HERMES_TOOLS, and each call is answered by the evaluation's
tool_call(fixture, name, args), with the case's fixture (HERMES_FIXTURE, written
by the runner before each case). evaluate.py imports only the standard library.
"""

import importlib.util
import json
import os
import sys

PROTOCOL = "2025-06-18"


def _evaluation():
    spec = importlib.util.spec_from_file_location("brick_evaluation", os.environ["HERMES_EVALUATE"])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    evaluation = _evaluation()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method, ident = message.get("method"), message.get("id")
        if ident is None:
            continue
        if method == "initialize":
            result = {"protocolVersion": (message.get("params") or {}).get("protocolVersion") or PROTOCOL,
                      "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "chip", "version": "1"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": list(evaluation.HERMES_TOOLS)}
        elif method == "tools/call":
            params = message.get("params") or {}
            with open(os.environ["HERMES_FIXTURE"]) as handle:
                fixture = json.load(handle)
            try:
                answer = evaluation.tool_call(fixture, str(params.get("name")), params.get("arguments") or {})
            except Exception as exc:  # noqa: BLE001 -- a failed tool is an error result, not a crash
                answer = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            result = {"content": [{"type": "text", "text": json.dumps(answer, ensure_ascii=False)}],
                      "isError": isinstance(answer, dict) and "error" in answer}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": ident,
                                         "error": {"code": -32601, "message": f"unknown method {method!r}"}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
