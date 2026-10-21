"""The evaluator's side of a computer use session: screenshots out, real input events in.

The model sees the page as a person does, and the actions go to the page as
real mouse and keyboard events (Playwright's page.mouse and page.keyboard).
A native file picker cannot be used on a virtual screen, so when the page
opens one, the picker gets the task's files and the model is told so; the
session records how many pickers opened.
"""

from __future__ import annotations

import base64
import itertools
import json
from typing import Any

SETTLE_MS = 300
MAX_SIDE = 2576                 # the API refuses a larger screenshot
MAX_PIXELS = 3_750_000
NOT_RUN = "Not executed: an earlier computer action in this turn failed."
MAX_WAIT_S = 10
KEYS = {"ctrl": "Control", "control": "Control", "alt": "Alt", "shift": "Shift", "super": "Meta", "cmd": "Meta",
        "meta": "Meta", "win": "Meta", "return": "Enter", "enter": "Enter", "kp_enter": "Enter",
        "backspace": "Backspace", "escape": "Escape", "esc": "Escape", "tab": "Tab", "delete": "Delete",
        "space": " ", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
        "page_up": "PageUp", "page_down": "PageDown", "pageup": "PageUp", "pagedown": "PageDown",
        "home": "Home", "end": "End", "insert": "Insert"}
CLICKS = {"left_click": ("left", 1), "right_click": ("right", 1), "middle_click": ("middle", 1),
          "double_click": ("left", 2), "triple_click": ("left", 3)}
CHANGES = {"left_click", "right_click", "middle_click", "double_click", "triple_click", "left_click_drag",
           "left_mouse_up", "scroll", "type", "key", "hold_key", "wait"}


def key_name(text: str) -> str:
    """xdotool-style names ("ctrl+a", "Return") to Playwright's ("Control+a", "Enter")."""
    parts = []
    for part in str(text).split("+"):
        low = part.strip().lower()
        if low in KEYS:
            parts.append(KEYS[low])
        elif len(low) > 1 and low[0] == "f" and low[1:].isdigit():
            parts.append(low.upper())
        else:
            parts.append(part.strip())
    return "+".join(parts)


class Agent:
    """Given to evaluate.observe(..., agent=). ``run`` does one task with the model."""

    def __init__(self, ask) -> None:
        self._ask = ask   # one message to the runner -> its reply
        self._names = itertools.count()

    def run(self, page, task: str, files: list[dict[str, Any]] | tuple = ()) -> dict[str, Any]:
        """Do ``task`` on ``page``. Returns {"status", "text", "steps", "actions", "pickers"}.

        status is "done" or "failed" (the model's own report), or "error" (a
        limit, or an answer the runner could not use). ``files`` are given to
        any file picker the page opens: [{"name", "mimeType", "buffer"}].
        """
        session = f"s{next(self._names)}"
        state = {"pickers": 0, "x": 0, "y": 0}

        def picker(chooser) -> None:
            state["pickers"] += 1
            chooser.set_files(list(files))

        page.on("filechooser", picker)
        actions: list[str] = []
        try:
            reply = self._ask({"session": session, "task": task, "results": [{"image": self._shot(page)}]})
            while "actions" in reply:
                results = []
                failed = False
                for action in reply["actions"]:
                    if failed:
                        # After a failed action the rest of the turn is not done: a click that missed,
                        # then typing, would type into the wrong place.
                        results.append({"id": action.get("id"), "text": NOT_RUN, "is_error": True})
                        continue
                    actions.append(action.get("name", "?"))
                    before = state["pickers"]
                    try:
                        result = self._do(page, action, state)
                    except Exception as exc:   # a bad coordinate, a closed page: the model is told
                        result = {"text": f"error: {type(exc).__name__}: {str(exc)[:200]}", "is_error": True}
                    failed = bool(result.get("is_error"))
                    if state["pickers"] > before:
                        note = ("A file picker opened; it selected " + ", ".join(f["name"] for f in files) + "."
                                if files else "A file picker opened; it was closed with no file.")
                        result["text"] = (result.get("text", "") + " " + note).strip()
                    results.append({"id": action.get("id"), **result})
                reply = self._ask({"session": session, "results": results})
        finally:
            page.remove_listener("filechooser", picker)
        if "error" in reply:
            return {"status": "error", "text": str(reply["error"])[:500], "steps": len(actions),
                    "actions": actions[:200], "pickers": state["pickers"]}
        text = str(reply.get("text", ""))
        word = text.split(None, 1)[0].strip(".:,").upper() if text.strip() else ""
        return {"status": "done" if word == "DONE" else "failed", "text": text[:500], "steps": len(actions),
                "actions": actions[:200], "pickers": state["pickers"]}

    @staticmethod
    def _shot(page, clip: dict[str, float] | None = None) -> str:
        # CSS pixels: the model's coordinates are then the coordinates page.mouse uses.
        size = page.viewport_size or {"width": 0, "height": 0}
        if max(size["width"], size["height"]) > MAX_SIDE or size["width"] * size["height"] > MAX_PIXELS:
            raise ValueError(f"the viewport {size['width']}x{size['height']} is too large for a screenshot")
        return base64.b64encode(page.screenshot(clip=clip, animations="allow", caret="initial", scale="css")).decode()

    def _do(self, page, action: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        name, args = action.get("name"), action.get("input") or {}
        mouse, keyboard = page.mouse, page.keyboard

        def at(key: str = "coordinate") -> tuple[float, float]:
            point = args.get(key)
            if point is None:
                return state["x"], state["y"]
            x, y = float(point[0]), float(point[1])
            state["x"], state["y"] = x, y
            return x, y

        def modifiers() -> list[str]:
            text = args.get("text")
            return [key_name(m) for m in str(text).split("+")] if text else []

        held = modifiers() if name in CLICKS or name in ("left_click_drag", "scroll") else []
        for key in held:
            keyboard.down(key)
        try:
            if name == "screenshot":
                return {"image": self._shot(page)}
            if name in CLICKS:
                button, count = CLICKS[name]
                x, y = at()
                mouse.click(x, y, button=button, click_count=count)
            elif name == "left_click_drag":
                sx, sy = at("start_coordinate")
                mouse.move(sx, sy)
                mouse.down()
                x, y = at()
                mouse.move(x, y, steps=12)
                mouse.up()
            elif name == "mouse_move":
                x, y = at()
                mouse.move(x, y)
            elif name == "left_mouse_down":
                mouse.down()
            elif name == "left_mouse_up":
                mouse.up()
            elif name == "cursor_position":
                return {"text": f"X={int(state['x'])}, Y={int(state['y'])}"}
            elif name == "scroll":
                x, y = at()
                mouse.move(x, y)
                amount = max(1, min(int(args.get("scroll_amount", 3)), 30)) * 100
                dx, dy = {"up": (0, -amount), "down": (0, amount), "left": (-amount, 0),
                          "right": (amount, 0)}[str(args.get("scroll_direction", "down"))]
                mouse.wheel(dx, dy)
            elif name == "type":
                keyboard.type(str(args.get("text", ""))[:4000], delay=8)
            elif name == "key":
                for _ in range(max(1, min(int(args.get("repeat", 1)), 100))):
                    keyboard.press(key_name(args.get("text", "")))
            elif name == "hold_key":
                key = key_name(args.get("text", ""))
                keyboard.down(key)
                page.wait_for_timeout(min(float(args.get("duration", 1)), MAX_WAIT_S) * 1000)
                keyboard.up(key)
            elif name == "wait":
                page.wait_for_timeout(min(float(args.get("duration", 1)), MAX_WAIT_S) * 1000)
            else:
                return {"text": f"error: {name!r} is not available here", "is_error": True}
        finally:
            for key in reversed(held):
                keyboard.up(key)
        if name in CHANGES:
            # After a change the model gets the new screen at once: one call less per step.
            page.wait_for_timeout(SETTLE_MS)
            return {"image": self._shot(page)}
        return {"text": "OK"}


def channel(stream, inbox):
    """The worker's half of the agent protocol: a message out, the runner's reply in."""
    def ask(message: dict[str, Any]) -> dict[str, Any]:
        stream.write(json.dumps({"op": "agent", **message}) + "\n")
        stream.flush()
        line = inbox.readline()
        if not line:
            raise RuntimeError("the runner closed the agent channel")
        return json.loads(line)
    return ask
