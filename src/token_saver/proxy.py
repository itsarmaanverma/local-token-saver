"""Local loopback proxy: Claude -> token-saver -> pxpipe -> Anthropic."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .proxy_support import (
    dashboard_page,
    dedupe_body,
    filter_retrieve,
    merge_usage,
    scan_sse,
    serialize_body,
)
from .stats import (
    Event,
    append_event,
    default_proxy_log_path,
    default_pxpipe_log_path,
    load_events,
    summarize_events,
    summarize_pxpipe_events,
)

DEFAULT_PORT = 47820
DEFAULT_UPSTREAM = "http://127.0.0.1:47821"
DEFAULT_MODE = "shadow"
VALID_MODES = ("off", "shadow", "dedupe", "retrieve")
DEFAULT_MAX_BODY = 128 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
PROBE_TIMEOUT = 5.0
FORWARD_TIMEOUT = 600.0
CLIENT_READ_TIMEOUT = 30.0
MAX_JSON_USAGE_BODY = 8 * 1024 * 1024
PROBE_HEADERS = (
    "x-api-key",
    "authorization",
    "anthropic-version",
    "anthropic-beta",
    "content-type",
)
HOP_BY_HOP = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class RequestBodyError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        addr,
        handler,
        *,
        mode: str,
        upstream: str,
        log_path,
        max_body_bytes: int = DEFAULT_MAX_BODY,
        pxpipe_log=None,
    ):
        parsed = urlsplit(upstream)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("upstream must be an http(s) URL with a host")
        if parsed.username or parsed.password:
            raise ValueError("upstream credentials must be supplied in request headers")
        super().__init__(addr, handler)
        self.mode = mode
        self.upstream_url = upstream
        self.upstream = parsed
        self.log_path = Path(log_path)
        self.pxpipe_log = Path(pxpipe_log) if pxpipe_log else default_pxpipe_log_path()
        self.max_body_bytes = max_body_bytes


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(CLIENT_READ_TIMEOUT)

    def log_message(self, _format, *args):
        return

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def do_PUT(self):
        self._route()

    def do_DELETE(self):
        self._route()

    def do_PATCH(self):
        self._route()

    def do_OPTIONS(self):
        self._route()

    def do_HEAD(self):
        self._route()

    def _read_body(self) -> bytes:
        transfer = self.headers.get("Transfer-Encoding")
        if transfer:
            raise RequestBodyError(400, "transfer-encoded request bodies are not supported")
        raw_lengths: list[str] = []
        for value in self.headers.get_all("Content-Length", []):
            raw_lengths.extend(part.strip() for part in value.split(","))
        if not raw_lengths:
            return b""
        try:
            lengths = {int(value, 10) for value in raw_lengths}
        except (TypeError, ValueError):
            raise RequestBodyError(400, "invalid Content-Length") from None
        if len(lengths) != 1 or next(iter(lengths)) < 0:
            raise RequestBodyError(400, "conflicting Content-Length")
        length = next(iter(lengths))
        if length > self.server.max_body_bytes:
            raise RequestBodyError(413, "request body exceeds configured limit")
        try:
            body = self.rfile.read(length)
        except socket.timeout:
            raise RequestBodyError(408, "request body timed out") from None
        if len(body) != length:
            raise RequestBodyError(400, "request body ended early")
        return body

    def _route(self):
        self._response_started = False
        self._upstream_status = None
        parsed_target = urlsplit(self.path)
        try:
            body = self._read_body()
        except RequestBodyError as exc:
            self.close_connection = True
            return self._local_json(exc.status, {"error": str(exc)})

        if self.command == "GET" and parsed_target.path == "/health":
            return self._local_json(200, {
                "ok": True,
                "mode": self.server.mode,
                "upstream": self.server.upstream_url,
            })
        if self.command == "GET" and parsed_target.path == "/":
            return self._dashboard()
        if self.command == "POST" and parsed_target.path == "/v1/messages":
            return self._handle_messages(parsed_target.path, body)
        try:
            self._forward(self.command, self.path, body, capture_usage=False)
        except Exception:
            if not self._response_started:
                self._safe_502()
            else:
                self.close_connection = True

    def _handle_messages(self, path: str, body: bytes):
        original_text = body.decode("utf-8", "replace")
        original_chars = len(original_text)
        force_off = self.headers.get("x-token-saver", "").strip().lower() == "off"
        try:
            parsed = json.loads(body) if body else None
        except (json.JSONDecodeError, ValueError, TypeError):
            parsed = None
        model = parsed.get("model") if isinstance(parsed, dict) else None
        mode = "off" if force_off else self.server.mode
        reason = mode
        error = None
        count = 0
        forwarded_body = body
        filtered_body = None
        filtered_chars = original_chars

        if mode != "off" and not isinstance(parsed, dict):
            reason, error = "error", "json_parse_failed"
        elif isinstance(parsed, dict) and mode in ("shadow", "dedupe", "retrieve"):
            count, _saved = dedupe_body(parsed, apply=True)
            if count > 0:
                if mode == "retrieve":
                    parsed = filter_retrieve(parsed)
                candidate = serialize_body(parsed)
                filtered_body = candidate.encode("utf-8")
                filtered_chars = len(candidate)
                if mode != "shadow":
                    forwarded_body = filtered_body

        probe: dict[str, object] = {}
        threads: list[threading.Thread] = []
        if count > 0 and filtered_body is not None:
            for probe_body, key in ((body, "baseline"), (filtered_body, "filtered")):
                thread = threading.Thread(
                    target=self._probe, args=(probe_body, probe, key), daemon=True
                )
                thread.start()
                threads.append(thread)

        status = None
        usage: dict[str, object] = {}
        try:
            status, usage = self._forward(
                self.command, self.path, forwarded_body, capture_usage=True
            )
        except Exception:
            status = self._upstream_status or 502
            reason = "error"
            error = error or (
                "upstream_stream_failed" if self._response_started else "upstream_forward_failed"
            )
            if not self._response_started:
                self._safe_502()
            else:
                self.close_connection = True

        for thread in threads:
            thread.join(timeout=PROBE_TIMEOUT + 1.0)
        append_event(self.server.log_path, Event(
            ts=int(time.time() * 1000),
            method=self.command,
            path=path,
            status=status,
            model=model,
            req_body_sha8=hashlib.sha256(forwarded_body).hexdigest()[:8],
            mode=mode,
            reason=reason,
            orig_chars=original_chars,
            filtered_chars=filtered_chars,
            blocks_deduped=count or None,
            baseline_tokens=probe.get("baseline_tokens"),
            baseline_probe_status=probe.get("baseline_status"),
            filtered_probe_tokens=probe.get("filtered_tokens"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_create_tokens=usage.get("cache_create_tokens"),
            cache_create_5m_tokens=usage.get("cache_create_5m_tokens"),
            cache_create_1h_tokens=usage.get("cache_create_1h_tokens"),
            cache_read_tokens=usage.get("cache_read_tokens"),
            stop_reason=usage.get("stop_reason"),
            error=error,
        ))

    def _open_upstream(self, timeout: float):
        upstream = self.server.upstream
        host = upstream.hostname or "127.0.0.1"
        if upstream.scheme == "https":
            return http.client.HTTPSConnection(host, upstream.port or 443, timeout=timeout)
        return http.client.HTTPConnection(host, upstream.port or 80, timeout=timeout)

    @staticmethod
    def _connection_tokens(headers) -> set[str]:
        tokens: set[str] = set()
        for value in headers.get_all("Connection", []):
            tokens.update(part.strip().lower() for part in value.split(",") if part.strip())
        return tokens

    def _forward_headers(self, body_len: int):
        connection_tokens = self._connection_tokens(self.headers)
        output = []
        for key in self.headers.keys():
            lowered = key.lower()
            if (
                lowered in HOP_BY_HOP
                or lowered in connection_tokens
                or lowered.startswith("proxy-")
                or lowered in ("x-token-saver", "content-length", "accept-encoding")
            ):
                continue
            output.extend((key, value) for value in self.headers.get_all(key, []))
        output.extend((("Accept-Encoding", "identity"), ("Content-Length", str(body_len))))
        return output

    def _upstream_target(self, target: str) -> str:
        parsed = urlsplit(target)
        prefix = self.server.upstream.path.rstrip("/")
        path = prefix + (parsed.path if parsed.path.startswith("/") else "/" + parsed.path)
        return path + (f"?{parsed.query}" if parsed.query else "")

    def _forward(self, method: str, target: str, body: bytes, *, capture_usage: bool):
        conn = self._open_upstream(FORWARD_TIMEOUT)
        try:
            conn.putrequest(method, self._upstream_target(target), skip_accept_encoding=True)
            for key, value in self._forward_headers(len(body)):
                conn.putheader(key, value)
            conn.endheaders(body if body else None)
            response = conn.getresponse()
            self._upstream_status = response.status
            connection_tokens = self._connection_tokens(response.headers)
            headers = []
            content_length = False
            content_type = ""
            content_encoding = ""
            for key, value in response.getheaders():
                lowered = key.lower()
                if lowered in HOP_BY_HOP or lowered in connection_tokens or lowered.startswith("proxy-"):
                    continue
                if lowered == "content-length":
                    content_length = True
                elif lowered == "content-type":
                    content_type = value.lower()
                elif lowered == "content-encoding":
                    content_encoding = value.lower()
                headers.append((key, value))
            is_sse = "text/event-stream" in content_type
            can_parse = not content_encoding or content_encoding == "identity"

            self._response_started = True
            self.send_response_only(response.status, response.reason)
            for key, value in headers:
                self.send_header(key, value)
            if not content_length:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            usage: dict[str, object] = {}
            sse_carry = bytearray() if capture_usage and is_sse and can_parse else None
            json_body = bytearray() if capture_usage and not is_sse and can_parse else None
            reader = getattr(response, "read1", response.read)
            while True:
                chunk = reader(CHUNK_SIZE)
                if not chunk:
                    break
                if content_length:
                    self.wfile.write(chunk)
                else:
                    self.wfile.write(b"%X\r\n" % len(chunk) + chunk + b"\r\n")
                if is_sse:
                    self.wfile.flush()
                if sse_carry is not None:
                    scan_sse(sse_carry, chunk, usage)
                elif json_body is not None and len(json_body) < MAX_JSON_USAGE_BODY:
                    remaining = MAX_JSON_USAGE_BODY - len(json_body)
                    json_body.extend(chunk[:remaining])
            if not content_length:
                self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            if json_body:
                try:
                    merge_usage(json.loads(bytes(json_body)), usage)
                except (json.JSONDecodeError, ValueError):
                    pass
            return response.status, usage
        finally:
            conn.close()

    def _probe(self, body: bytes, results: dict, key: str) -> None:
        try:
            conn = self._open_upstream(PROBE_TIMEOUT)
            try:
                headers = {
                    name: self.headers.get(name)
                    for name in PROBE_HEADERS
                    if self.headers.get(name) is not None
                }
                headers.setdefault("content-type", "application/json")
                headers["accept-encoding"] = "identity"
                conn.putrequest("POST", self._upstream_target("/v1/messages/count_tokens"),
                                skip_accept_encoding=True)
                for name, value in headers.items():
                    conn.putheader(name, value)
                conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(body if body else None)
                response = conn.getresponse()
                data = response.read()
                if key == "baseline":
                    results["baseline_status"] = f"http_{response.status}"
                if response.status == 200:
                    tokens = json.loads(data).get("input_tokens")
                    if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
                        results[f"{key}_tokens"] = tokens
                        if key == "baseline":
                            results["baseline_status"] = "ok"
            finally:
                conn.close()
        except Exception as exc:
            if key == "baseline":
                results["baseline_status"] = type(exc).__name__

    def _local_json(self, status: int, obj: dict) -> None:
        payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self._response_started = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _safe_502(self) -> None:
        if not self._response_started:
            self._local_json(502, {"error": "upstream unavailable"})

    def _dashboard(self) -> None:
        token_summary = summarize_events(load_events(self.server.log_path))
        px_summary = summarize_pxpipe_events(load_events(self.server.pxpipe_log))
        payload = dashboard_page(
            self.server.mode,
            self.server.upstream_url,
            str(self.server.log_path),
            token_summary,
            px_summary,
        )
        self._response_started = True
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _env_int(parser: argparse.ArgumentParser, name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        parser.error(f"{name} must be an integer")
    if value <= 0:
        parser.error(f"{name} must be positive")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="token-saver-proxy", description="token-saver chain proxy")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--upstream", default=None)
    parser.add_argument("--mode", choices=VALID_MODES, default=None)
    parser.add_argument("--log-path", default=None)
    parser.add_argument("--max-body-mb", type=int, default=None)
    args = parser.parse_args(argv)
    port = args.port if args.port is not None else _env_int(parser, "TOKEN_SAVER_PORT", DEFAULT_PORT)
    if not 1 <= port <= 65535:
        parser.error("port must be between 1 and 65535")
    mode = args.mode or os.environ.get("TOKEN_SAVER_FILTER", DEFAULT_MODE)
    if mode not in VALID_MODES:
        parser.error(f"TOKEN_SAVER_FILTER must be one of: {', '.join(VALID_MODES)}")
    if args.max_body_mb is not None and args.max_body_mb <= 0:
        parser.error("--max-body-mb must be positive")
    max_mb = args.max_body_mb if args.max_body_mb is not None else _env_int(
        parser, "TOKEN_SAVER_MAX_BODY_MB", DEFAULT_MAX_BODY // 1024 // 1024
    )
    try:
        server = ProxyServer(
            ("127.0.0.1", port),
            ProxyHandler,
            mode=mode,
            upstream=args.upstream or os.environ.get("TOKEN_SAVER_UPSTREAM", DEFAULT_UPSTREAM),
            log_path=args.log_path or default_proxy_log_path(),
            max_body_bytes=max_mb * 1024 * 1024,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(
        f"token-saver-proxy listening on http://127.0.0.1:{port} "
        f"(mode={mode}, upstream={server.upstream_url}, log={server.log_path})",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("token-saver-proxy shutting down", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
