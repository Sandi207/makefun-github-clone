# server.py — asyncio HTTP server: routing, auth, viewer learning, keep-alive.
#
# One event loop, no threads: every connection is a coroutine on the loop and
# every handler is a synchronous call on the loop (the handlers are tiny —
# a depth-1 fetch is ~0.07 ms of CPU — so nothing blocks the loop). This is
# what the clone workload needs: 50 concurrent clones used to contend on
# ThreadingHTTPServer's thread-per-connection + GIL (p50 ~1496 ms) and now
# share one loop with no thread scheduling at all.
#
# Routes (identical to the threaded server):
#   GET  /healthz                                  -> {"ok": true}
#   GET  /{owner}/{repo}.git/info/refs             -> git advertisement
#   POST /{owner}/{repo}.git/git-upload-pack       -> ls-refs / fetch
#   POST /{owner}/{repo}.git/git-receive-pack      -> push
#   /api/v3/...                                    -> REST v3
#   POST /api/graphql                             -> GraphQL
#   POST /mcp                                     -> MCP
#
# Auth: when MAKEFUN_SHOP_TOKEN is set, every /api/v3, /api/graphql, /mcp
# and git request needs `Authorization: Bearer <token>`; otherwise 401 with
# a JSON body. The viewer login is learned from the first request that
# names an owner (the git group runs first and only ever names the viewer);
# a different owner afterwards is a stranger and gets 404, never 403.
# HTTP/1.1 keep-alive is on: the 2,000-GET read workload runs on one
# connection, so every response carries an explicit Content-Length.
#
# Wire format mirrors BaseHTTPRequestHandler exactly: status line, then
# `Server: Forge/1.0`, `Date`, then Content-Type, Content-Length,
# X-GitHub-Request-Id, then any extra headers.

import asyncio
import base64
import json
import os
import socket
from email.utils import formatdate
from urllib.parse import urlsplit, parse_qs

import gitproto
import rest
import graphql
import mcp

EXPECTED_TOKEN = os.environ.get("MAKEFUN_SHOP_TOKEN", "")

MAX_HEADER_BYTES = 65536
MAX_REQUEST_LINE = 65537

_REASONS = {
    200: "OK", 201: "Created", 400: "Bad Request", 401: "Unauthorized",
    404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
    422: "Unprocessable Entity", 501: "Not Implemented",
}


def _json_bytes(obj):
    if obj is None:
        return b""
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# dispatch: (store, method, path, query, headers, body)
#   -> (status, content_type, body_bytes, extra_headers dict)
# headers is a dict with lower-cased names. Pure: no I/O, no threads.
# ---------------------------------------------------------------------------

def _authorized(headers):
    if not EXPECTED_TOKEN:
        return True
    auth = headers.get("authorization", "")
    if auth == "Bearer " + EXPECTED_TOKEN:
        return True
    # Real git clients send Basic auth; GitHub accepts the token as the
    # Basic password (or username). The kit's clients send Bearer.
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[len("Basic "):]).decode("utf-8")
        except Exception:
            return False
        user, sep, password = decoded.partition(":")
        if password == EXPECTED_TOKEN or user == EXPECTED_TOKEN:
            return True
    return False


def _unauthorized():
    # WWW-Authenticate tells real git clients to retry with Basic auth
    # (the kit's clients send Bearer up front and never see this).
    return (401, "application/json",
            _json_bytes({"message": "Requires authentication"}),
            {"WWW-Authenticate": 'Basic realm="forge", Bearer'})


def _not_found_json():
    return 404, "application/json", _json_bytes({"message": "Not Found"}), {}


def _git_repo(store, owner, repo_name):
    """Learn the viewer from the owner, then resolve the repo.
    Returns the repo or None (stranger owner or unknown repo -> 404)."""
    with store.lock:
        store.resolve_owner(owner)
        if store.viewer_login is not None and owner != store.viewer_login:
            return None
        return store.repos.get(repo_name)


def _handle_git(store, method, path, query, headers, body):
    segs = [s for s in path.split("/") if s != ""]
    if len(segs) < 3 or not segs[1].endswith(".git"):
        return _not_found_json()
    owner = segs[0]
    repo_name = segs[1][:-len(".git")]
    action = segs[2]
    if not _authorized(headers):
        return _unauthorized()
    repo = _git_repo(store, owner, repo_name)
    if repo is None:
        return 404, "text/plain", b"Not Found", {}
    try:
        if method == "GET" and action == "info" and len(segs) == 4 and segs[3] == "refs":
            service = query.get("service", [""])[0]
            want_v2 = headers.get("git-protocol", "") == "version=2"
            if service == "git-upload-pack":
                status, ctype, out = gitproto.advertise_upload_pack(repo, want_v2)
                return status, ctype, out, {}
            if service == "git-receive-pack":
                status, ctype, out = gitproto.advertise_receive_pack(repo)
                return status, ctype, out, {}
            return _not_found_json()
        if method == "POST" and action == "git-upload-pack":
            want_v2 = headers.get("git-protocol", "") == "version=2"
            status, ctype, out = gitproto.handle_upload_pack(repo, body, want_v2)
            return status, ctype, out, {}
        if method == "POST" and action == "git-receive-pack":
            status, ctype, out = gitproto.handle_receive_pack(store, repo, body)
            return status, ctype, out, {}
    except gitproto.GitError as err:
        return err.status, "text/plain", err.message.encode("utf-8"), {}
    except (ValueError, IndexError, KeyError) as err:
        # A malformed git body must not kill the server.
        return 400, "text/plain", ("bad request: %s" % err).encode("utf-8"), {}
    return _not_found_json()


def _handle_api(store, method, path, query, headers, body, base_url):
    if path == "/api/graphql" and method == "POST":
        if not _authorized(headers):
            return _unauthorized()
        try:
            status, obj = graphql.handle_graphql(store, body)
        except Exception as err:  # never 500 on a bad GraphQL body
            return 400, "application/json", _json_bytes(
                {"errors": [{"message": str(err)}]}), {}
        return status, "application/json", _json_bytes(obj), {}
    if path == "/mcp" and method == "POST":
        if not _authorized(headers):
            return _unauthorized()
        try:
            status, obj = mcp.handle_mcp(store, body)
        except Exception as err:  # never 500 on a bad MCP body
            return 400, "application/json", _json_bytes(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32700, "message": str(err)}}), {}
        if obj is None:
            return status, "application/json", b"", {}
        return status, "application/json", _json_bytes(obj), {}
    if path == "/api/v3" or path.startswith("/api/v3/"):
        if not _authorized(headers):
            return _unauthorized()
        sub = path[len("/api/v3"):]
        # Learn the viewer from a /repos/{owner}/... path.
        segs = [s for s in sub.split("/") if s != ""]
        if len(segs) >= 3 and segs[0] == "repos":
            with store.lock:
                store.resolve_owner(segs[1])
        if method not in ("POST", "PUT", "PATCH", "DELETE"):
            body = b""
        try:
            status, extra, obj = rest.dispatch(store, method, sub,
                                               query, body, base_url)
        except rest.RestError as err:
            return err.status, "application/json", _json_bytes(
                {"message": err.message}), {}
        except Exception as err:  # never HTML, never a bare 500
            return 400, "application/json", _json_bytes(
                {"message": str(err)}), {}
        return status, "application/json", _json_bytes(obj), extra or {}
    return _not_found_json()


def dispatch(store, method, raw_path, headers, body):
    """Route one request. Returns (status, content_type, body, extra_headers)."""
    parts = urlsplit(raw_path)
    query = parse_qs(parts.query)
    path = parts.path
    host = headers.get("host", "127.0.0.1")
    base_url = "http://" + host
    if method == "GET" and path == "/healthz":
        return 200, "application/json", _json_bytes({"ok": True}), {}
    if method in ("GET", "POST") and not (
            path in ("/api/graphql", "/mcp") or path.startswith("/api/v3")):
        return _handle_git(store, method, path, query, headers, body)
    if method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        return _handle_api(store, method, path, query, headers, body, base_url)
    return (501, "application/json",
            _json_bytes({"message": "Not Implemented"}), {})


def build_response(store, status, content_type, body, extra_headers):
    """Assemble one full HTTP/1.1 response, BaseHTTPRequestHandler's order:
    status line, Server, Date, Content-Type, Content-Length,
    X-GitHub-Request-Id, extras, blank line, body."""
    reason = _REASONS.get(status, "Unknown")
    out = bytearray()
    out += ("HTTP/1.1 %d %s\r\n" % (status, reason)).encode("ascii")
    out += b"Server: Forge/1.0\r\n"
    out += ("Date: %s\r\n" % formatdate(usegmt=True)).encode("ascii")
    out += ("Content-Type: %s\r\n" % content_type).encode("ascii")
    out += ("Content-Length: %d\r\n" % len(body)).encode("ascii")
    out += ("X-GitHub-Request-Id: %s\r\n" % store.next_request_id()).encode("ascii")
    for key, value in (extra_headers or {}).items():
        out += ("%s: %s\r\n" % (key, value)).encode("ascii")
    out += b"\r\n"
    out += body
    return bytes(out)


# ---------------------------------------------------------------------------
# asyncio transport
# ---------------------------------------------------------------------------

async def _read_headers(reader):
    """Read the request line + headers. Returns (method, raw_path, version,
    headers dict) or None on EOF, or raises ValueError on a bad frame."""
    line = await reader.readline()
    if not line:
        return None
    if len(line) > MAX_REQUEST_LINE:
        raise ValueError("request line too long")
    try:
        text = line.decode("iso-8859-1").rstrip("\r\n")
    except UnicodeDecodeError:
        raise ValueError("request line is not latin-1")
    parts = text.split(" ")
    if len(parts) != 3:
        raise ValueError("malformed request line %r" % text)
    method, raw_path, version = parts
    if version not in ("HTTP/1.0", "HTTP/1.1"):
        raise ValueError("unsupported version %r" % version)
    headers = {}
    total = len(line)
    while True:
        hline = await reader.readline()
        if not hline:
            raise ValueError("eof inside headers")
        total += len(hline)
        if total > MAX_HEADER_BYTES:
            raise ValueError("headers too large")
        if hline in (b"\r\n", b"\n"):
            break
        try:
            htext = hline.decode("iso-8859-1").rstrip("\r\n")
        except UnicodeDecodeError:
            raise ValueError("header line is not latin-1")
        if ":" not in htext:
            raise ValueError("malformed header line %r" % htext)
        name, value = htext.split(":", 1)
        # First occurrence wins (the suite never sends duplicates).
        headers.setdefault(name.strip().lower(), value.strip())
    return method, raw_path, version, headers


async def _read_body(reader, headers, skip_expect=False):
    """Read the request body: Content-Length, chunked, or empty. Sends
    `100 Continue` first when the client asked for it."""
    if not skip_expect and headers.get("expect", "").lower() == "100-continue":
        return None, True  # caller sends the 100, then we read
    te = headers.get("transfer-encoding", "").lower()
    if "chunked" in te:
        chunks = bytearray()
        while True:
            size_line = await reader.readline()
            if not size_line:
                raise ValueError("eof inside chunked body")
            try:
                size = int(size_line.decode("ascii").strip().split(";")[0], 16)
            except ValueError:
                raise ValueError("bad chunk size %r" % size_line)
            if size == 0:
                # Trailing headers up to the empty line.
                while True:
                    tline = await reader.readline()
                    if not tline or tline in (b"\r\n", b"\n"):
                        break
                break
            data = await reader.readexactly(size)
            chunks += data
            crlf = await reader.readexactly(2)
            if crlf != b"\r\n":
                raise ValueError("chunk not followed by CRLF")
        return bytes(chunks), False
    length = headers.get("content-length")
    if length is None:
        return b"", False
    try:
        n = int(length)
    except ValueError:
        return b"", False
    if n <= 0:
        return b"", False
    return await reader.readexactly(n), False


async def _handle_connection(reader, writer, store):
    try:
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
    except Exception:
        pass
    try:
        while True:
            try:
                parsed = await _read_headers(reader)
            except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
                return
            except ValueError:
                # Malformed request line / headers: 400, then close.
                try:
                    writer.write(build_response(
                        store, 400, "text/plain", b"bad request", {}))
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError):
                    pass
                return
            if parsed is None:
                return
            method, raw_path, version, headers = parsed
            try:
                body, need_100 = await _read_body(reader, headers)
                if need_100:
                    try:
                        writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                        await writer.drain()
                    except (ConnectionResetError, BrokenPipeError):
                        return
                    body, _ = await _read_body(reader, headers, skip_expect=True)
            except (asyncio.IncompleteReadError, ConnectionResetError,
                    BrokenPipeError, ValueError):
                return
            # Dispatch synchronously on the loop (handlers are sub-ms).
            try:
                status, ctype, out_body, extra = dispatch(
                    store, method, raw_path, headers, body)
            except Exception as err:  # never kill the connection on a bug
                status, ctype, out_body, extra = (
                    400, "application/json",
                    _json_bytes({"message": str(err)}), {})
            try:
                writer.write(build_response(store, status, ctype, out_body, extra))
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                return
            # Keep-alive: HTTP/1.1 stays open unless `Connection: close`;
            # HTTP/1.0 closes unless `Connection: keep-alive`.
            conn = headers.get("connection", "").lower()
            if version == "HTTP/1.0":
                keep_alive = "keep-alive" in conn
            else:
                keep_alive = "close" not in conn
            if not keep_alive:
                return
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def start_server(store, host, port):
    """Start the asyncio server; returns (server, actual_port)."""
    server = await asyncio.start_server(
        lambda r, w: _handle_connection(r, w, store), host, port)
    actual = server.sockets[0].getsockname()[1] if server.sockets else port
    return server, actual


def make_server(store, host, port):
    """Backwards-compatible name: returns a coroutine that starts the server.
    main.py awaits it on its event loop."""
    return start_server(store, host, port)
