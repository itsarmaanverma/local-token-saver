"""Tests for the token-saver chain proxy (src/token_saver/proxy.py).

Stdlib + pytest, ephemeral ports. A stub upstream (ThreadingHTTPServer) records
raw request bytes and serves canned responses:

* ``POST /v1/messages``            -> canned JSON (usage) or, when the request
                                      body has ``"stream": true``, a canned SSE
                                      stream (close-delimited, so the proxy's
                                      chunked re-framing path is exercised).
* ``POST /v1/messages/count_tokens`` -> canned ``{"input_tokens": N}`` (the
                                      unbilled baseline/filtered probe target).

The proxy is pointed at the stub via its ``upstream`` argument; each test spins
a fresh chain on ephemeral ports.
"""
import contextlib
import http.client
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from token_saver import proxy as proxy_mod

# ---------------------------------------------------------------------------
# Canned payloads
# ---------------------------------------------------------------------------

BIG = "X" * 2500  # > DEDUPE_MIN_CHARS (2000)
SMALL = "y" * 1500  # < 2000, never deduped
COUNT_TOKENS_INPUT = 123
STREAM_DELAY = 0.8

MESSAGES_JSON = json.dumps({
    "id": "msg_stub",
    "type": "message",
    "role": "assistant",
    "model": "claude-3-5-sonnet-20241022",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {
        "input_tokens": 40,
        "output_tokens": 8,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    },
}).encode("utf-8")

SSE_BODY = (
    b"event: message_start\n"
    b'data: {"type":"message_start","message":{"id":"msg_sse","type":"message",'
    b'"role":"assistant","model":"claude-3-5-sonnet-20241022","usage":'
    b'{"input_tokens":55,"output_tokens":1,"cache_creation_input_tokens":0,'
    b'"cache_read_input_tokens":0}}}\n\n'
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","index":0,"delta":'
    b'{"type":"text_delta","text":"Hello"}}\n\n'
    b"event: message_delta\n"
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    b'"usage":{"output_tokens":9}}\n\n'
    b"event: message_stop\n"
    b'data: {"type":"message_stop"}\n\n'
)


class StubState:
    def __init__(self):
        self.requests = []  # dicts: method, path, body, headers


def _make_stub_handler(state: StubState):
    class Stub(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"  # close-delimited: no keep-alive

        def log_message(self, *a):
            return

        def _record(self) -> bytes:
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
            except (TypeError, ValueError):
                n = 0
            body = self.rfile.read(n) if n else b""
            state.requests.append({
                "method": self.command,
                "path": self.path,
                "body": body,
                "headers": {k.lower(): v for k, v in self.headers.items()},
            })
            return body

        def _send(self, payload: bytes, ctype: str, with_length: bool = True):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            if with_length:
                self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            body = self._record()
            if self.path == "/v1/messages/count_tokens":
                return self._send(
                    json.dumps({"input_tokens": COUNT_TOKENS_INPUT}).encode(),
                    "application/json",
                )
            if self.path == "/v1/messages":
                try:
                    j = json.loads(body)
                except (json.JSONDecodeError, ValueError):
                    j = {}
                if j.get("stream"):
                    if j.get("delayed_stream"):
                        split = SSE_BODY.index(b"event: content_block_delta")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        self.wfile.write(SSE_BODY[:split])
                        self.wfile.flush()
                        time.sleep(STREAM_DELAY)
                        self.wfile.write(SSE_BODY[split:])
                        self.wfile.flush()
                        return
                    return self._send(SSE_BODY, "text/event-stream", with_length=False)
                return self._send(MESSAGES_JSON, "application/json")
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Stub


@contextlib.contextmanager
def chain(mode: str, log_path=None, max_body_bytes=proxy_mod.DEFAULT_MAX_BODY):
    """Start stub upstream + proxy on ephemeral ports; yield (proxy_port, state)."""
    state = StubState()
    stub = ThreadingHTTPServer(("127.0.0.1", 0), _make_stub_handler(state))
    stub_port = stub.server_address[1]
    upstream = f"http://127.0.0.1:{stub_port}"
    if log_path is None:
        log_path = os.devnull
    proxy = proxy_mod.ProxyServer(
        ("127.0.0.1", 0), proxy_mod.ProxyHandler,
        mode=mode, upstream=upstream, log_path=str(log_path),
        max_body_bytes=max_body_bytes,
    )
    proxy_port = proxy.server_address[1]
    t_stub = threading.Thread(target=stub.serve_forever, daemon=True)
    t_proxy = threading.Thread(target=proxy.serve_forever, daemon=True)
    t_stub.start()
    t_proxy.start()
    try:
        yield proxy_port, state
    finally:
        proxy.shutdown()
        proxy.server_close()
        stub.shutdown()
        stub.server_close()


def req(port, method, path, body=b"", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.request(method, path, body=body, headers=dict(headers or {}))
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
    finally:
        conn.close()


def messages_bodies(state: StubState):
    return [r["body"] for r in state.requests if r["path"] == "/v1/messages"]


def read_events(path):
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def wait_for_events(path, predicate, timeout=3.0):
    """Poll the log file until predicate(events) is true (the proxy logs after
    the response is flushed, so there is a brief race)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            evs = read_events(path)
        except FileNotFoundError:
            evs = []
        if predicate(evs):
            return evs
        time.sleep(0.02)
    return read_events(path) if _exists(path) else []


def _exists(path):
    try:
        open(path, "r").close()
        return True
    except OSError:
        return False


def dup_body():
    """A request body with two identical > 2k tool_result blocks."""
    return json.dumps({
        "model": "claude-3-5-sonnet-20241022",
        "max_tokens": 100,
        "messages": [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": BIG}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "b", "content": BIG}]},
        ],
    }).encode("utf-8")


# ---------------------------------------------------------------------------
# 1. shadow: upstream receives byte-identical body; response intact
# ---------------------------------------------------------------------------


def test_shadow_forwards_byte_identical_body_and_response():
    body = dup_body()
    with chain("shadow") as (port, state):
        status, headers, data = req(
            port, "POST", "/v1/messages", body,
            {"content-type": "application/json"},
        )
    assert status == 200
    assert data == MESSAGES_JSON  # response passed through intact
    fwd = messages_bodies(state)
    assert len(fwd) == 1
    assert fwd[0] == body  # shadow never mutates the forwarded body


# ---------------------------------------------------------------------------
# 2. dedupe: later identical >2k tool_result replaced; determinism; logged
# ---------------------------------------------------------------------------


def test_dedupe_replaces_duplicate_and_is_deterministic(tmp_path):
    log = tmp_path / "events.jsonl"
    body = dup_body()
    with chain("dedupe", log_path=log) as (port, state):
        req(port, "POST", "/v1/messages", body, {"content-type": "application/json"})
        req(port, "POST", "/v1/messages", body, {"content-type": "application/json"})
        evs = wait_for_events(
            str(log),
            lambda e: sum(1 for r in e if r.get("path") == "/v1/messages") >= 2,
        )

    fwd = messages_bodies(state)
    assert len(fwd) == 2
    first = fwd[0].decode("utf-8")
    # first occurrence kept intact, second replaced by a stable stub
    assert BIG in first
    assert "token-saver: duplicate of sha8=" in first
    assert first.count(BIG) == 1  # exactly one full copy remains
    # determinism: identical input -> byte-identical forwarded output
    assert fwd[0] == fwd[1]

    proxy_rows = [r for r in evs if r.get("path") == "/v1/messages"]
    assert proxy_rows
    assert all(r.get("mode") == "dedupe" for r in proxy_rows)
    assert all(r.get("blocks_deduped", 0) >= 1 for r in proxy_rows)


# ---------------------------------------------------------------------------
# 3. sub-2k untouched; count_tokens passthrough byte-identical; x-token-saver off
# ---------------------------------------------------------------------------


def test_sub_2k_duplicates_not_deduped():
    body = json.dumps({
        "model": "claude-3-5-sonnet-20241022",
        "messages": [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": SMALL}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "b", "content": SMALL}]},
        ],
    }).encode("utf-8")
    with chain("dedupe") as (port, state):
        req(port, "POST", "/v1/messages", body, {"content-type": "application/json"})
    fwd = messages_bodies(state)[0].decode("utf-8")
    assert "token-saver: duplicate" not in fwd  # nothing replaced
    assert fwd.count(SMALL) == 2  # both small copies survive


def test_active_mode_without_candidates_preserves_original_json_bytes():
    body = b'{ "model": "x", "messages": [{"role":"user","content":"hi"}] }\n'
    with chain("dedupe") as (port, state):
        req(port, "POST", "/v1/messages", body, {"content-type": "application/json"})
    assert messages_bodies(state) == [body]


def test_count_tokens_passthrough_byte_identical_even_in_dedupe():
    body = dup_body()  # even a dedupe-able body must pass through untouched here
    with chain("dedupe") as (port, state):
        status, _h, _d = req(
            port, "POST", "/v1/messages/count_tokens", body,
            {"content-type": "application/json"},
        )
    assert status == 200
    ct = [r for r in state.requests if r["path"] == "/v1/messages/count_tokens"]
    assert len(ct) == 1
    assert ct[0]["body"] == body  # count_tokens is never filtered


def test_x_token_saver_off_forces_passthrough():
    body = dup_body()
    with chain("dedupe") as (port, state):
        req(port, "POST", "/v1/messages", body,
            {"content-type": "application/json", "x-token-saver": "off"})
    fwd = messages_bodies(state)
    assert len(fwd) == 1
    assert fwd[0] == body  # off -> original bytes
    # and the control header is stripped before forwarding
    msg_reqs = [r for r in state.requests if r["path"] == "/v1/messages"]
    assert "x-token-saver" not in msg_reqs[0]["headers"]


# ---------------------------------------------------------------------------
# 4. SSE response streams through byte-exact
# ---------------------------------------------------------------------------


def test_sse_response_byte_exact():
    body = json.dumps({
        "model": "claude-3-5-sonnet-20241022",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode("utf-8")
    with chain("shadow") as (port, state):
        status, headers, data = req(
            port, "POST", "/v1/messages", body,
            {"content-type": "application/json"},
        )
    assert status == 200
    assert "text/event-stream" in headers.get("content-type", "")
    assert data == SSE_BODY  # byte-exact passthrough of the SSE payload


def test_sse_first_chunk_is_not_buffered_until_stream_close():
    body = json.dumps({
        "model": "claude-3-5-sonnet-20241022",
        "stream": True,
        "delayed_stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode("utf-8")
    with chain("shadow") as (port, _state):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("POST", "/v1/messages", body=body,
                         headers={"content-type": "application/json"})
            response = conn.getresponse()
            started = time.monotonic()
            first = response.read(1)
            first_byte_seconds = time.monotonic() - started
            rest = response.read()
        finally:
            conn.close()
    assert first + rest == SSE_BODY
    assert first_byte_seconds < STREAM_DELAY / 2


def test_mixed_media_tool_results_are_never_text_deduped():
    body = json.dumps({
        "model": "claude-3-5-sonnet-20241022",
        "messages": [
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "a",
                "content": [
                    {"type": "text", "text": BIG},
                    {"type": "image", "source": {"data": "first"}},
                ],
            }]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "b",
                "content": [
                    {"type": "text", "text": BIG},
                    {"type": "image", "source": {"data": "second"}},
                ],
            }]},
        ],
    }).encode("utf-8")
    with chain("dedupe") as (port, state):
        req(port, "POST", "/v1/messages", body, {"content-type": "application/json"})
    forwarded = json.loads(messages_bodies(state)[0])
    encoded = json.dumps(forwarded)
    assert encoded.count(BIG) == 2
    assert '"data": "first"' in encoded
    assert '"data": "second"' in encoded
    assert "token-saver: duplicate" not in encoded


def _raw_request(port, request_bytes):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(request_bytes)
        client.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = client.recv(4096)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)


def test_invalid_and_oversized_request_framing_is_rejected():
    with chain("shadow", max_body_bytes=8) as (port, state):
        invalid = _raw_request(
            port,
            b"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\n"
            b"Content-Length: nope\r\nConnection: close\r\n\r\n",
        )
        oversized = _raw_request(
            port,
            b"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\n"
            b"Content-Length: 9\r\nConnection: close\r\n\r\n123456789",
        )
        chunked = _raw_request(
            port,
            b"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\n"
            b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n0\r\n\r\n",
        )
        identity = _raw_request(
            port,
            b"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\n"
            b"Transfer-Encoding: identity\r\nConnection: close\r\n\r\n{}",
        )
    assert b" 400 " in invalid.split(b"\r\n", 1)[0]
    assert b" 413 " in oversized.split(b"\r\n", 1)[0]
    assert b" 400 " in chunked.split(b"\r\n", 1)[0]
    assert b" 400 " in identity.split(b"\r\n", 1)[0]
    assert not state.requests


def test_connection_named_headers_are_stripped_and_identity_is_requested():
    body = json.dumps({"model": "x", "messages": []}).encode()
    with chain("off") as (port, state):
        req(port, "POST", "/v1/messages", body, {
            "content-type": "application/json",
            "connection": "x-remove",
            "x-remove": "secret",
            "accept-encoding": "gzip",
        })
    headers = [row for row in state.requests if row["path"] == "/v1/messages"][0]["headers"]
    assert "x-remove" not in headers
    assert headers["accept-encoding"] == "identity"


def test_main_rejects_invalid_limits_and_upstream_before_binding():
    with pytest.raises(SystemExit):
        proxy_mod.main(["--max-body-mb", "0"])
    with pytest.raises(SystemExit):
        proxy_mod.main(["--upstream", "file:///tmp/socket"])


# ---------------------------------------------------------------------------
# 5. /health ok; event line written to a tmp log (--log-path)
# ---------------------------------------------------------------------------


def test_health_and_event_logging(tmp_path):
    log = tmp_path / "events.jsonl"
    body = dup_body()
    with chain("shadow", log_path=log) as (port, state):
        hstatus, _h, hdata = req(port, "GET", "/health")
        assert hstatus == 200
        health = json.loads(hdata)
        assert health["ok"] is True
        assert health["mode"] == "shadow"
        assert health["upstream"].startswith("http://127.0.0.1:")

        req(port, "POST", "/v1/messages", body, {"content-type": "application/json"})
        evs = wait_for_events(str(log), lambda e: any(r.get("path") == "/v1/messages" for r in e))

    rows = [r for r in evs if r.get("path") == "/v1/messages"]
    assert rows
    r = rows[0]
    assert r["method"] == "POST"
    assert r["mode"] == "shadow"
    assert r["status"] == 200
    assert r.get("blocks_deduped", 0) >= 1  # the duplicate was counted (candidate)
