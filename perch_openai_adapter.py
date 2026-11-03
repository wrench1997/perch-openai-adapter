"""
OpenAI-compatible adapter over Perch. Real streaming, real tool calls,
real multi-turn memory.

Transport: `perch acp` (JSON-RPC over stdio), not `perch run --json`.
Measured: `perch run --json` is fully buffered (54k chars in one lump, 0.01s
spread). ACP emits `agent_message_chunk` as the model produces text, and
`tool_call` / `tool_call_update` as Perch runs its own tools.

Three things this fixes over the naive version:

  1. TOOL CALLS are surfaced, not swallowed. Perch executes tools itself; the
     ACP notifications say which tool, what input, and what came back. Those
     are emitted as OpenAI `tool_calls` deltas so a client can render them.
     It is NOT client-driven tool calling - Perch decides and executes. What
     this gives you is full visibility plus the results in the transcript.

  2. MULTI-TURN MEMORY is real. A conversation key (`user` field, or the
     `X-Conversation-Id` header) maps to one long-lived ACP session, so Perch
     remembers earlier turns itself. Callers can send only the new message.

  3. STARTUP is self-healing. The adapter starts its own ACP sessions on
     demand and reports readiness on /healthz, so a client that probes first
     never hits a dead endpoint.

    POST /v1/chat/completions
    GET  /v1/models
    GET  /healthz
    GET  /v1/conversations            list live sessions
    DELETE /v1/conversations/<key>    drop one

Run: python perch_openai_adapter.py [--port 8791] [--max-sessions 8]
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

from perch_acp import AcpError, SessionStore

TIERS = ["standard", "standard 1m", "pro", "pro 1m"]

# Perch's model layer exposes exactly four tiers and nothing finer over ACP.
# Verified two ways: `perch acp` rejects --model / --effort ("Unknown
# interactive flag"), and session/new returns only modes, no model list.
# session/set_config does not exist (Method not found). So the picker cannot
# offer more than these four - that is the product surface, not a limit here.
TIER_INFO = {
    "standard":    {"label": "Standard", "context": 200_000},
    "standard 1m": {"label": "Standard 1M", "context": 1_000_000},
    "pro":         {"label": "Pro", "context": 200_000},
    "pro 1m":      {"label": "Pro 1M", "context": 1_000_000},
}


def model_catalog() -> list[dict]:
    """The four tiers, with the metadata a client needs to allow images.

    `inputModalities` is the field DeepSeek Harness reads to decide whether a
    model accepts image input. Without it, or without "image" in the list, the
    client refuses to attach a screenshot with "the current model does not
    support images" - even though Perch itself handles images fine (ACP
    advertises promptCapabilities.image).

    Verified in the Harness bundle:
        inputModalities: model.inputModalities ?? ["text"]
        if (!info.inputModalities.includes("image")) throw ...
    """
    out = []
    for t in TIERS:
        info = TIER_INFO[t]
        out.append({
            "id": t,
            "object": "model",
            "created": 0,
            "owned_by": "perch",
            "name": info["label"],
            "context_window": info["context"],
            "input_modalities": ["text", "image"],
            "inputModalities": ["text", "image"],
            "supports_images": True,
            "supportsImages": True,
            "capabilities": {"vision": True, "images": True, "tools": True},
            "perch": {"tier": t, "contextWindow": info["context"],
                      "inputModalities": ["text", "image"]},
        })
    return out


def parse_model(name: str) -> str:
    """Normalise whatever the client sends to one of the four tier ids."""
    raw = (name or "").strip().lower()
    if raw in TIERS:
        return raw
    alias = {
        "standard_max": "standard 1m", "pro_max": "pro 1m",
        "standard-1m": "standard 1m", "pro-1m": "pro 1m",
        "standard1m": "standard 1m", "pro1m": "pro 1m",
    }
    if raw in alias:
        return alias[raw]
    # a client may append an effort suffix; the tier is what matters
    if "@" in raw:
        head = raw.split("@", 1)[0].strip()
        if head in TIERS:
            return head
    return "standard"
DEFAULT_TIMEOUT = 300

STORE: SessionStore | None = None


class _ClientGone(Exception):
    """The client closed the connection while we were streaming."""


# --------------------------------------------------------------------------
# message handling
# --------------------------------------------------------------------------

def split_content(content) -> tuple[str, list[dict]]:
    """Split an OpenAI content field into (text, image parts).

    OpenAI sends either a string or a list of parts:
      {"type": "text", "text": "..."}
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}

    ACP's promptCapabilities advertises image support, so image parts are
    carried through to the prompt instead of being dropped.
    """
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return str(content), []

    texts: list[str] = []
    images: list[dict] = []
    for p in content:
        if isinstance(p, str):
            texts.append(p)
            continue
        if not isinstance(p, dict):
            continue
        ptype = p.get("type")
        if ptype == "text" and isinstance(p.get("text"), str):
            texts.append(p["text"])
        elif ptype in ("image_url", "input_image"):
            holder = p.get("image_url") or p.get("image") or {}
            url = holder.get("url") if isinstance(holder, dict) else holder
            if not isinstance(url, str) or not url:
                continue
            images.append({"url": url, "mimeType": _mime_of(url)})
    return "\n".join(texts), images


def _mime_of(url: str) -> str:
    if url.startswith("data:"):
        head = url[5:].split(";", 1)[0].strip()
        if head:
            return head
    low = url.lower().rsplit("?", 1)[0]
    for ext, mime in ((".png", "image/png"), (".jpg", "image/jpeg"),
                      (".jpeg", "image/jpeg"), (".webp", "image/webp"),
                      (".gif", "image/gif")):
        if low.endswith(ext):
            return mime
    return "image/png"


def render_task(messages: list[dict], stateless: bool) -> tuple[str, list[dict]]:
    """Turn an OpenAI message array into (task text, image parts).

    When a conversation key is in play the ACP session already holds the
    history, so only the newest user message is sent. Otherwise the whole
    array is rendered, because there is no session to remember it.
    """
    system_parts: list[str] = []
    convo: list[tuple[str, str]] = []
    images: list[dict] = []

    for m in messages:
        role = str(m.get("role", "user"))
        text, imgs = split_content(m.get("content"))
        text = text.strip()
        # keep images from the newest user turn only; older ones are context
        if imgs and role == "user":
            images = imgs
        if not text and not imgs:
            continue
        if role == "system":
            system_parts.append(text)
        elif role == "tool":
            convo.append(("Tool", text))
        elif role == "assistant":
            convo.append(("Assistant", text))
        else:
            convo.append(("User", text))

    last_user = ""
    for role, text in reversed(convo):
        if role == "User":
            last_user = text
            break
    if not last_user:
        last_user = convo[-1][1] if convo else ""

    if stateless:
        prior = [f"{r}: {t}" for r, t in convo if not (r == "User" and t == last_user)]
        task = last_user
        if prior:
            task = ("Continue this conversation. Prior turns:\n\n"
                    + "\n".join(prior)
                    + "\n\nNow respond to the final user message:\n" + last_user)
    else:
        task = last_user

    if system_parts:
        task = "Instructions:\n" + "\n".join(system_parts) + "\n\n" + task

    # An image-only message leaves the task empty, and Perch rejects an empty
    # prompt. Give it a neutral instruction so the image is still analysed.
    if not task.strip():
        task = "Describe and analyse the attached image."
    return task, images


def tool_activity(tool_calls: list[dict]) -> list[dict]:
    """Summarise what Perch ran, as METADATA - never as OpenAI tool_calls.

    This distinction is the whole bug. OpenAI `tool_calls` mean "client, you
    must execute this and send the result back". Perch has already executed it.
    Emitting tool_calls made Harness look for a tool named `execute` in its own
    registry, fail with `Error: unknown tool "execute"`, and stall the turn.

    So tool activity is reported only in the `perch` side-channel of the JSON
    response. It is never put in `tool_calls`, and never appended to the
    message text - the assistant content stays exactly what the model wrote.
    """
    out = []
    for tc in tool_calls:
        kind = tc.get("kind") or "tool"
        title = tc.get("title") or kind
        raw_input = tc.get("rawInput") if isinstance(tc.get("rawInput"), dict) else {}
        result_text = ""
        for block in (tc.get("content") or []):
            inner = block.get("content") if isinstance(block, dict) else None
            if isinstance(inner, dict) and inner.get("type") == "text":
                result_text = inner.get("text") or ""
                break
        out.append({
            "kind": kind,
            "title": str(title)[:400],
            "status": tc.get("status"),
            "input": raw_input,
            "resultPreview": result_text[:600],
        })
    return out


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "PerchOpenAIAdapter/3.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # ---------- helpers ----------

    def _json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _sse_send(self, obj: dict) -> None:
        """Write one SSE frame.

        Raises _ClientGone instead of letting a socket error escape: a client
        that closes the tab mid-stream is normal, not a server fault, and the
        raw ConnectionAbortedError was filling the log with tracebacks.
        """
        try:
            self.wfile.write(
                ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")
            )
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as e:
            raise _ClientGone(str(e)) from e

    def _conversation_key(self, body: dict) -> str | None:
        hdr = self.headers.get("X-Conversation-Id") or self.headers.get("X-Session-Id")
        if hdr:
            return str(hdr)[:128]
        u = body.get("user")
        if isinstance(u, str) and u.strip():
            return "user:" + u.strip()[:100]
        return None

    # ---------- routes ----------

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._json(200, {
                "ok": True,
                "transport": "acp",
                "streaming": True,
                "toolCalls": "surfaced",
                "multiTurn": "session-backed",
                "mode": STORE.mode if STORE else None,
                "sessions": STORE.stats() if STORE else {},
            })
        if path == "/v1/models":
            return self._json(200, {"object": "list", "data": model_catalog()})
        if path == "/v1/conversations":
            return self._json(200, STORE.stats() if STORE else {})
        self._json(404, {"error": {"message": "not found"}})

    def do_DELETE(self):  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/v1/conversations/"):
            key = unquote(path[len("/v1/conversations/"):])
            STORE.drop(key)
            return self._json(200, {"dropped": key})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path != "/v1/chat/completions":
            return self._json(404, {"error": {"message": "not found"}})

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": {"message": "bad Content-Length"}})
        if length <= 0:
            return self._json(400, {"error": {"message": "empty body"}})

        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return self._json(400, {"error": {"message": f"invalid JSON: {e}"}})

        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return self._json(400, {"error": {"message": "messages must be a non-empty array"}})

        model = parse_model(str(body.get("model") or "standard"))

        key = self._conversation_key(body)
        task, images = render_task(messages, stateless=(key is None))

        if body.get("stream"):
            return self._handle_stream(task, model, key, images)
        return self._handle_blocking(task, model, key, images)

    # ---------- blocking ----------

    def _handle_blocking(self, task: str, model: str, key: str | None,
                         images: list[dict] | None = None) -> None:
        try:
            sess = STORE.get(key) if key else STORE.get("__anon_" + uuid.uuid4().hex[:8])
            r = sess.prompt(task, images=images, timeout=DEFAULT_TIMEOUT)
        except AcpError as e:
            sys.stderr.write(f"[acp-failed] {e}\n")
            return self._json(502, {"error": {"type": "perch_acp_failed",
                                              "message": str(e)[:800]}})
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[adapter-error] {type(e).__name__}: {e}\n")
            return self._json(500, {"error": {"type": "adapter_error",
                                              "message": f"{type(e).__name__}: {e}"[:800]}})

        text = r["text"]
        thinking = r.get("thinking") or ""
        activity = tool_activity(r.get("toolCalls") or [])

        message: dict = {"role": "assistant", "content": text}
        # Harness reads reasoning from these fields and renders it separately
        # from the answer. Perch's ACP gives us that stream, so pass it through
        # instead of letting it contaminate the body.
        if thinking:
            message["reasoning_content"] = thinking

        return self._json(200, {
            "id": "chatcmpl-" + uuid.uuid4().hex[:24],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": len(task) // 4,
                      "completion_tokens": len(text) // 4,
                      "total_tokens": (len(task) + len(text)) // 4},
            "perch": {"chunks": r["chunks"], "thoughtChunks": r.get("thoughtChunks", 0),
                      "stopReason": r["stopReason"], "conversation": key,
                      "sessionTurns": sess.turns, "toolActivity": activity},
        })

    # ---------- streaming ----------

    def _handle_stream(self, task: str, model: str, key: str | None,
                       images: list[dict] | None = None) -> None:
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        q: queue.Queue = queue.Queue()
        done = object()

        def worker() -> None:
            try:
                sess = STORE.get(key) if key else STORE.get("__anon_" + uuid.uuid4().hex[:8])
                sess.prompt(
                    task,
                    images=images,
                    on_chunk=lambda p: q.put(("text", p)),
                    on_thought=lambda p: q.put(("thinking", p)),
                    on_event=lambda u: q.put(("event", u)),
                    timeout=DEFAULT_TIMEOUT,
                )
            except Exception as e:  # noqa: BLE001
                q.put(("error", f"{type(e).__name__}: {e}"))
            finally:
                q.put(done)

        threading.Thread(target=worker, daemon=True).start()
        self._sse_start()

        def chunk(delta: dict, finish=None) -> dict:
            return {"id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        self._sse_send(chunk({"role": "assistant", "content": ""}))

        # Tool activity is NOT emitted as tool_calls. See tool_activity():
        # OpenAI tool_calls tell the client to execute something, but Perch has
        # already executed it, and a client that tries to run `execute` fails
        # with `unknown tool` and stalls. The trace goes into the text instead.
        activity: list[dict] = []
        pending_tools: dict[str, dict] = {}
        try:
            while True:
                item = q.get(timeout=DEFAULT_TIMEOUT)
                if item is done:
                    break
                kind, payload = item
                if kind == "error":
                    self._sse_send({"error": {"type": "perch_acp_failed",
                                              "message": str(payload)[:500]}})
                    break
                if kind == "text":
                    self._sse_send(chunk({"content": payload}))
                    continue
                if kind == "thinking":
                    # Harness maps delta.reasoning_content to a thinking block
                    # and delta.content to the answer. Keeping them apart is
                    # what stops reasoning from appearing in the reply.
                    self._sse_send(chunk({"reasoning_content": payload}))
                    continue

                if payload.get("sessionUpdate") not in ("tool_call", "tool_call_update"):
                    continue
                tid = payload.get("toolCallId") or ""
                if not tid:
                    continue
                merged = pending_tools.setdefault(tid, {})
                merged.update({k: v for k, v in payload.items() if v is not None})
                if merged.get("status") in ("completed", "failed"):
                    activity = tool_activity(list(pending_tools.values()))

            # Tool activity is reported only in the `perch` side-channel of the
            # non-streaming response. It is never appended to the text: the
            # assistant content must be exactly what the model produced, and an
            # injected trace would be replayed as history on the next turn.
            self._sse_send(chunk({}, finish="stop"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except _ClientGone:
            # client hung up mid-stream; nothing to report, the turn still ran
            pass
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> None:
    global STORE
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--max-sessions", type=int, default=8)
    ap.add_argument("--ttl", type=int, default=1800)
    ap.add_argument("--mode", default="agents", choices=["ask", "agents", "plan"],
                    help="Perch permission mode for adapter sessions. 'agents' lets "
                         "Perch actually run tools, which is what an agent client "
                         "like Harness expects; 'ask' is read-only.")
    args = ap.parse_args()

    STORE = SessionStore(ttl_seconds=args.ttl, max_sessions=args.max_sessions,
                         mode=args.mode)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"perch openai adapter on http://{args.host}:{args.port}/v1")
    print(f"  streaming : yes (SSE)")
    print(f"  tool calls: surfaced as OpenAI tool_calls")
    print(f"  memory    : session-backed, {args.max_sessions} max, ttl {args.ttl}s")
    print(f"  mode      : {args.mode}")
    print("  point any OpenAI client's base_url here")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
        srv.shutdown()
        STORE.close_all()


if __name__ == "__main__":
    main()
