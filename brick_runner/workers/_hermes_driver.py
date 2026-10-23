"""The Hermes driver: runs inside the sandbox with Hermes's own Python (see brick_runner/hermes.py).

argv[1] is the Hermes checkout. HERMES_HOME holds the runner's config.yaml and
.env (ANTHROPIC_BASE_URL is the model relay). This file starts a small TCP relay,
127.0.0.1:8099 -> the Unix socket model.sock beside it (the sandbox has no
network), then answers one request on each stdin line, one JSON line on stdout:

    {"op": "reply", "system", "messages", "soul", "now"} -> {"text", "steps", "tools"} or {"error"}

With "now" (ISO 8601 with an offset), Hermes's clock (hermes_time.now) is that
moment for the request, so its prompt names the day the case is set in.

It builds the agent as Hermes's API server does (gateway.run's helpers and the
api_server platform toolsets), so the evaluation runs the same agent as CHIP.
"""

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import threading

MNT = Path(os.environ.get("HERMES_MNT", "/mnt"))
OUT = sys.stdout


def _relay() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 8099))
    server.listen(16)

    def pipe(src, dst) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def serve() -> None:
        while True:
            client, _ = server.accept()
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            upstream.connect(str(MNT / "model.sock"))
            threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()


def _say(data: dict) -> None:
    OUT.write(json.dumps(data) + "\n")
    OUT.flush()


def main() -> None:
    checkout = sys.argv[1]
    sys.path.insert(0, checkout)
    sys.stdout = sys.stderr   # Hermes prints; only _say writes the protocol
    _relay()
    try:
        from run_agent import AIAgent
        from gateway.run import (_checkpoint_agent_kwargs, _current_max_iterations, _load_gateway_config,
                                 _resolve_gateway_model, _resolve_runtime_agent_kwargs)
        from hermes_cli.tools_config import _get_platform_tools
        from tools.mcp_tool_discovery import discover_mcp_tools
        discovered = discover_mcp_tools()   # the gateway does this at start: connect the fixture tools
    except Exception as exc:  # noqa: BLE001
        _say({"error": f"start: {type(exc).__name__}: {exc}"[:400]})
        return
    if not any(name.startswith("mcp__chip__") for name in discovered or []):
        _say({"error": f"the fixture tools did not connect (found {sorted(discovered or [])[:10]})"})
        return
    import hermes_time
    real_now = hermes_time.now
    _say({"ready": True})
    home = Path(os.environ["HERMES_HOME"])
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            (home / "SOUL.md").write_text(str(request.get("soul") or ""))
            messages = [m for m in request.get("messages", []) if isinstance(m, dict)]
            if not messages or messages[-1].get("role") != "user":
                _say({"error": "the last message must be from the user"})
                continue
            moment = None
            try:
                moment = datetime.fromisoformat(str(request.get("now") or "").replace("Z", "+00:00"))
            except ValueError:
                pass
            session_id = None
            if moment is not None and moment.tzinfo is not None:
                hermes_time.now = lambda moment=moment: moment
                # The session id embeds its start time ("Conversation started:" in the prompt): the case's time,
                # and a hash of the request, so it is stable for the cache and unique for each case.
                digest = hashlib.sha256(line.encode()).hexdigest()[:8]
                session_id = f"{moment.strftime('%Y%m%d_%H%M%S')}_{digest}"
            else:
                hermes_time.now = real_now
            config = _load_gateway_config()
            runtime = _resolve_runtime_agent_kwargs()
            model = runtime.pop("model", None) or _resolve_gateway_model()
            runtime.pop("_fallback_notice", None)
            steps, tools = [], {}

            def start(call_id, name, args) -> None:
                if not name.startswith("_"):
                    steps.append({"tool": name, "label": ""})
                    tools[call_id] = {"name": name, "args": args if isinstance(args, dict) else {}, "result": None}

            def done(call_id, name, args, result) -> None:
                if call_id in tools:
                    tools[call_id]["result"] = str(result)[:4000]

            agent = AIAgent(model=model, **runtime, **_checkpoint_agent_kwargs(config),
                            max_iterations=_current_max_iterations(), quiet_mode=True, verbose_logging=False,
                            ephemeral_system_prompt=str(request.get("system") or "") or None,
                            enabled_toolsets=sorted(_get_platform_tools(config, "api_server")),
                            platform="api_server", tool_start_callback=start, tool_complete_callback=done,
                            **({"session_id": session_id} if session_id else {}),
                            session_db=None, skip_memory=True)
            result = agent.run_conversation(
                user_message=messages[-1].get("content", ""),
                conversation_history=[{"role": m.get("role"), "content": m.get("content", "")} for m in messages[:-1]])
            text = result.get("final_response") if isinstance(result, dict) else None
            _say({"text": str(text or ""), "steps": steps, "tools": list(tools.values())})
        except Exception as exc:  # noqa: BLE001 -- one failed case must not stop the next
            _say({"error": f"{type(exc).__name__}: {exc}"[:400]})


if __name__ == "__main__":
    main()
