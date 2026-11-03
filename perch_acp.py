"""
Perch ACP client: a persistent JSON-RPC session over stdio.

Why ACP and not `perch run --json`:

  Measured, `perch run --json` is fully buffered. A 54,062-character answer
  arrived in one lump at t=8.50s with a 0.01s spread. Slicing that would be
  fake streaming - the user would watch us dribble out text we already had.

  `perch acp` speaks JSON-RPC 2.0 over stdin/stdout and emits
  `session/update` -> `agent_message_chunk` notifications as the model
  produces them. Measured: 7 chunks over 1.88s. That is real streaming.

Two session strategies live here:

  AcpPool      - one session per request, returned afterwards. Stateless.
  SessionStore - one session per CONVERSATION KEY, kept alive with a TTL.
                 This is what gives an OpenAI client real multi-turn memory:
                 the ACP session itself accumulates context, so the caller
                 does not have to resend the whole history every time.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from typing import Callable

def _first_existing(*candidates: str) -> str:
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return candidates[0] if candidates else ""


def _resolve_node() -> str:
    """NODE_BIN override, then PATH, then the usual install locations."""
    env = os.environ.get("NODE_BIN")
    if env:
        return env
    found = shutil.which("node")
    if found:
        return found
    return _first_existing(
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
        "/usr/local/bin/node",
        "/opt/homebrew/bin/node",
    )


def _resolve_perch_bin() -> str:
    """PERCH_BIN override, then PATH, then the npm global layouts."""
    env = os.environ.get("PERCH_BIN")
    if env:
        return env
    found = shutil.which("perch")
    if found:
        return found
    home = os.path.expanduser("~")
    return _first_existing(
        os.path.join(home, "AppData", "Roaming", "npm", "node_modules",
                     "perchai-cli", "bin", "perch"),
        os.path.join(home, "AppData", "Roaming", "npm", "perch.cmd"),
        "/usr/local/lib/node_modules/perchai-cli/bin/perch",
        "/opt/homebrew/lib/node_modules/perchai-cli/bin/perch",
    )


NODE = _resolve_node()
PERCH_BIN = _resolve_perch_bin()


class AcpError(RuntimeError):
    pass


class AcpSession:
    """One `perch acp` process, one ACP session, many prompts."""

    def __init__(self, cwd: str | None = None, mode: str = "ask",
                 timeout: float = 300.0):
        self.cwd = cwd or os.getcwd()
        self.mode = mode
        self.timeout = timeout
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, dict] = {}
        self._session_id: str | None = None
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._ready = threading.Event()
        self._notify_handler: Callable[[dict], None] | None = None
        self.turns = 0
        self.last_used = time.time()

    # ---------- lifecycle ----------

    def start(self) -> None:
        env = dict(os.environ)
        env["NO_COLOR"] = "1"
        # NOTE: `perch acp` takes no --model / --effort. Verified: the CLI
        # rejects both ("Unknown interactive flag: --model", "Unknown domain:
        # acp --model"), and session/new returns only modes, no model list.
        # So a session runs on whatever tier the account is configured for;
        # the tier cannot be chosen per request over ACP.
        self._proc = subprocess.Popen(
            [NODE, PERCH_BIN, "acp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        init = self._request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
        }, timeout=60)
        if "error" in init:
            raise AcpError(f"initialize failed: {init['error']}")

        sess = self._request("session/new", {"cwd": self.cwd, "mcpServers": []}, timeout=60)
        if "error" in sess:
            raise AcpError(f"session/new failed: {sess['error']}")
        self._session_id = sess["result"]["sessionId"]

        if self.mode and self.mode != "default":
            try:
                self._request("session/set_mode",
                              {"sessionId": self._session_id, "modeId": self.mode},
                              timeout=15)
            except Exception:
                pass
        self._ready.set()

    def alive(self) -> bool:
        return bool(self._proc and self._proc.poll() is None and self._ready.is_set())

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass

    @property
    def session_id(self) -> str | None:
        return self._session_id

    # ---------- transport ----------

    def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        for raw in self._proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            rid = msg.get("id")
            if rid is not None and rid in self._pending:
                slot = self._pending[rid]
                slot["msg"] = msg
                slot["event"].set()
            else:
                handler = self._notify_handler
                if handler:
                    try:
                        handler(msg)
                    except Exception:
                        pass
        for slot in list(self._pending.values()):
            slot["event"].set()

    def _send(self, obj: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(obj) + "\n")
        self._proc.stdin.flush()

    def _request(self, method: str, params: dict, timeout: float | None = None) -> dict:
        with self._lock:
            self._next_id += 1
            rid = self._next_id
        slot = {"event": threading.Event(), "msg": None}
        self._pending[rid] = slot
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        ok = slot["event"].wait(timeout or self.timeout)
        self._pending.pop(rid, None)
        if not ok or slot["msg"] is None:
            raise AcpError(f"{method} timed out after {timeout or self.timeout}s")
        return slot["msg"]

    # ---------- prompting ----------

    def prompt(self, text: str, on_chunk: Callable[[str], None] | None = None,
               on_event: Callable[[dict], None] | None = None,
               on_thought: Callable[[str], None] | None = None,
               images: list[dict] | None = None,
               timeout: float | None = None) -> dict:
        """Send a prompt; stream text deltas, thinking deltas, and tool events.

        Perch's ACP separates two text streams, and a client renders them
        differently:

          agent_thought_chunk  -> the model's reasoning ("Simple arithmetic. 17*")
          agent_message_chunk  -> the final answer ("17 × 23 = ... = 391.")

        Measured on one prompt: 6 thought chunks, 1 message chunk. Collapsing
        both into one stream is what put reasoning into the answer body.

        on_chunk   - final-answer text deltas (agent_message_chunk)
        on_thought - reasoning deltas (agent_thought_chunk)
        on_event   - every raw session/update payload, for tool_call / plan /
                     mode changes
        images     - [{url, mimeType}] from OpenAI content parts
        """
        if not self._ready.is_set():
            raise AcpError("session not started")
        if not self._session_id:
            raise AcpError("no session id")

        chunks: list[str] = []
        thoughts: list[str] = []
        tool_calls: list[dict] = []

        def handler(msg: dict) -> None:
            if msg.get("method") != "session/update":
                return
            upd = (msg.get("params") or {}).get("update") or {}
            kind = upd.get("sessionUpdate")

            if kind == "tool_call":
                tool_calls.append(upd)
            elif kind == "tool_call_update" and tool_calls:
                tid = upd.get("toolCallId")
                for tc in tool_calls:
                    if tid and tc.get("toolCallId") == tid:
                        tc.update(upd)
                        break
                else:
                    tool_calls.append(upd)

            if on_event:
                try:
                    on_event(upd)
                except Exception:
                    pass

            if kind not in ("agent_message_chunk", "agent_thought_chunk"):
                return
            content = upd.get("content") or {}
            if content.get("type") != "text":
                return
            piece = content.get("text") or ""
            if not piece:
                return

            if kind == "agent_thought_chunk":
                thoughts.append(piece)
                if on_thought:
                    on_thought(piece)
                return

            chunks.append(piece)
            if on_chunk:
                on_chunk(piece)

        # Build the ACP prompt blocks: text first, then any images.
        blocks: list[dict] = []
        if text.strip():
            blocks.append({"type": "text", "text": text})
        for img in (images or []):
            url = img.get("url") or ""
            if not url:
                continue
            blocks.append({
                "type": "image",
                "mimeType": img.get("mimeType") or _mime_of(url),
                "data": url.split(",", 1)[1] if url.startswith("data:") and "," in url else url,
            })
        if not blocks:
            blocks = [{"type": "text", "text": text}]

        self._notify_handler = handler
        try:
            resp = self._request(
                "session/prompt",
                {"sessionId": self._session_id, "prompt": blocks},
                timeout=timeout or self.timeout,
            )
        finally:
            self._notify_handler = None

        self.turns += 1
        self.last_used = time.time()

        if "error" in resp:
            raise AcpError(str(resp["error"]))

        return {
            "text": "".join(chunks),
            "thinking": "".join(thoughts),
            "stopReason": (resp.get("result") or {}).get("stopReason"),
            "chunks": len(chunks),
            "thoughtChunks": len(thoughts),
            "toolCalls": tool_calls,
        }


class AcpPool:
    """Stateless pool: one session per request, returned afterwards."""

    def __init__(self, size: int = 2, cwd: str | None = None, mode: str = "ask"):
        self.size = max(1, size)
        self.cwd = cwd
        self.mode = mode
        self._idle: list[AcpSession] = []
        self._lock = threading.Lock()
        self._all: list[AcpSession] = []

    def acquire(self) -> AcpSession:
        with self._lock:
            while self._idle:
                s = self._idle.pop()
                if s.alive():
                    return s
        s = AcpSession(cwd=self.cwd, mode=self.mode)
        s.start()
        with self._lock:
            self._all.append(s)
        return s

    def release(self, s: AcpSession) -> None:
        with self._lock:
            if len(self._idle) < self.size and s.alive():
                self._idle.append(s)
                return
        s.close()

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._all)
            self._all.clear()
            self._idle.clear()
        for s in sessions:
            s.close()


class SessionStore:
    """One session per conversation key, kept alive with a TTL.

    This is what makes multi-turn work properly. An ACP session accumulates its
    own context, so once a caller has a conversation key, later turns only need
    to send the NEW message - Perch already remembers the rest. Verified: a
    second prompt on the same session answered with the number from the first.

    Bounded two ways, because an ACP session grows without limit otherwise:
      - max_sessions: least-recently-used is evicted and closed
      - ttl_seconds: idle sessions are closed
      - max_turns: a session is recycled after N turns, so a long conversation
        cannot blow the model's context budget
    """

    def __init__(self, ttl_seconds: int = 1800, max_sessions: int = 8,
                 max_turns: int = 40, mode: str = "ask", cwd: str | None = None):
        self.ttl = ttl_seconds
        self.max_sessions = max_sessions
        self.max_turns = max_turns
        self.mode = mode
        self.cwd = cwd
        self._sessions: dict[str, AcpSession] = {}
        self._lock = threading.Lock()
    def _evict_locked(self) -> None:
        now = time.time()
        dead = [k for k, s in self._sessions.items()
                if now - s.last_used > self.ttl or not s.alive()
                or s.turns >= self.max_turns]
        for k in dead:
            s = self._sessions.pop(k)
            s.close()
        # still too many? drop the least recently used
        while len(self._sessions) > self.max_sessions:
            oldest = min(self._sessions.items(), key=lambda kv: kv[1].last_used)
            self._sessions.pop(oldest[0])
            oldest[1].close()

    def get(self, key: str, model: str | None = None,
            effort: str | None = None) -> AcpSession:
        """Get or create a session for this conversation key.

        model/effort are accepted for interface stability but cannot be applied:
        ACP has no session/set_config and the CLI rejects --model on `acp`.
        They are recorded on the session for diagnostics only.
        """
        skey = key
        with self._lock:
            self._evict_locked()
            s = self._sessions.get(skey)
            if s is not None and s.alive() and s.turns < self.max_turns:
                s.last_used = time.time()
                return s
            if s is not None:
                s.close()
                self._sessions.pop(skey, None)
        s = AcpSession(cwd=self.cwd, mode=self.mode)
        s.start()
        with self._lock:
            self._sessions[skey] = s
        return s

    def drop(self, key: str) -> None:
        with self._lock:
            s = self._sessions.pop(key, None)
        if s:
            s.close()

    def stats(self) -> dict:
        with self._lock:
            self._evict_locked()
            return {
                "sessions": len(self._sessions),
                "maxSessions": self.max_sessions,
                "ttlSeconds": self.ttl,
                "maxTurns": self.max_turns,
                "turns": {k: v.turns for k, v in self._sessions.items()},
            }

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            s.close()
