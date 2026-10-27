"""A git smart-HTTP server with no artificial limits.

Requests are delegated to ``git http-backend`` (CGI).  Response bodies are
streamed in 64 KiB chunks and each connection gets its own thread, so there is
no cap on concurrent clones, fetches or pushes, and a 10 GiB repository never
costs 10 GiB of server memory.  Chunked uploads (git uses them above
``http.postBuffer``, 1 MiB by default) are spooled to a temporary file so the
backend can be given the exact ``CONTENT_LENGTH`` it requires, then deleted.
"""

from __future__ import annotations

import base64
import hmac
import html
import json
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from . import __version__
from .repo import GitusesError, Store, validate_name

CHUNK = 64 * 1024
REALM = "gituses"

_GIT_BACKEND = None
_GIT_BACKEND_LOCK = threading.Lock()


def git_http_backend() -> str:
    """Locate ``git-http-backend`` (override with GIT_HTTP_BACKEND)."""
    override = os.environ.get("GIT_HTTP_BACKEND")
    if override:
        return override
    global _GIT_BACKEND
    with _GIT_BACKEND_LOCK:
        if _GIT_BACKEND is None:
            try:
                exec_path = subprocess.run(
                    ["git", "--exec-path"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=True,
                    text=True,
                ).stdout.strip()
            except (OSError, subprocess.CalledProcessError) as exc:
                raise RuntimeError("git is required but was not found on PATH") from exc
            candidates = [
                os.path.join(exec_path, "git-http-backend"),
                "/usr/lib/git-core/git-http-backend",
                "/usr/libexec/git-core/git-http-backend",
            ]
            for candidate in candidates:
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    _GIT_BACKEND = candidate
                    break
            else:  # pragma: no cover - depends on the git package
                found = shutil.which("git-http-backend")
                if not found:
                    raise RuntimeError(
                        "git-http-backend not found; install the full git package"
                    )
                _GIT_BACKEND = found
    return _GIT_BACKEND


class GitusesHandler(BaseHTTPRequestHandler):
    server_version = "gituses/" + __version__
    protocol_version = "HTTP/1.0"

    # -- routing --------------------------------------------------------
    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def _dispatch(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        try:
            if path in ("/", "/index.html"):
                if self.command not in ("GET", "HEAD"):
                    return self._plain(405, "method not allowed\n")
                return self._index()
            if path == "/_health":
                body = json.dumps(
                    {
                        "status": "ok",
                        "repositories": len(self.server.store.list()),
                        "anonymous": not self.server.token,
                    }
                ).encode() + b"\n"
                return self._plain(200, body, "application/json")
            head = path.lstrip("/").partition("/")[0]
            if not head.endswith(".git"):
                return self._plain(404, "not found: expected /<repository>.git\n")
            try:
                validate_name(head)
                repo = self.server.store.repo_path(head)
            except GitusesError:
                return self._plain(404, "not found\n")
            if not self._authorised():
                return
            if not os.path.isdir(repo):
                return self._plain(404, "repository %s does not exist\n" % head)
            # git-http-backend expects the full request path in PATH_INFO,
            # rooted at GIT_PROJECT_ROOT (it parses the repository name itself).
            self._backend(path, parsed.query)
        except (BrokenPipeError, ConnectionResetError):  # client went away
            self.close_connection = True
        except Exception as exc:  # pragma: no cover - last resort logging
            self.log_error("gituses internal error: %s", exc)
            try:
                self._plain(500, "internal server error\n")
            except OSError:
                pass

    # -- auth -----------------------------------------------------------
    def _authorised(self) -> bool:
        token = self.server.token
        if not token:
            return True
        header = self.headers.get("Authorization", "") or ""
        supplied = ""
        if header.lower().startswith("bearer "):
            supplied = header[7:].strip()
        elif header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(header[6:].strip()).decode(
                    "utf-8", "replace"
                )
            except (ValueError, base64.binascii.Error):
                decoded = ""
            username, _, password = decoded.partition(":")
            # git prompts for a username, so accept the token in either slot.
            supplied = password or username
        if supplied and hmac.compare_digest(supplied, token):
            return True
        body = b"authentication required\n"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="%s"' % REALM)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
            self.wfile.flush()
        self.close_connection = True
        return False

    # -- git smart HTTP -------------------------------------------------
    def _backend(self, path_info: str, query: str) -> None:
        """Stream this request through ``git http-backend``."""
        env = os.environ.copy()
        env.update(
            {
                "GIT_PROJECT_ROOT": self.server.store.root,
                "GIT_HTTP_EXPORT_ALL": "1",
                "PATH_INFO": path_info,
                "REQUEST_METHOD": self.command,
                "QUERY_STRING": query,
                "REMOTE_USER": "gituses",
                "REMOTE_ADDR": self.client_address[0] if self.client_address else "",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": self.headers.get("Content-Length", ""),
                "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
                "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
                "HTTP_GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
            }
        )
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        chunked = "chunked" in (self.headers.get("Transfer-Encoding") or "").lower()

        # git-http-backend needs an exact CONTENT_LENGTH (git switches to
        # chunked uploads above http.postBuffer, 1 MiB by default).  Spool those
        # to a temporary file so arbitrarily large pushes stay within bounded
        # memory; the file is removed as soon as the push finishes.
        body = None
        if chunked:
            body = tempfile.TemporaryFile(prefix="gituses-body-")
            try:
                self._decode_chunked(body)
            except (OSError, ValueError, ConnectionError):
                body.close()
                return self._plain(400, "malformed chunked request body\n")
            length = body.tell()
            body.seek(0)
            env["CONTENT_LENGTH"] = str(length)
            env.pop("HTTP_TRANSFER_ENCODING", None)
            env.pop("HTTP_EXPECT", None)

        proc = subprocess.Popen(
            [git_http_backend()],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=self.server.store.root,
            bufsize=0,
        )
        # Pump the request body on a helper thread: a push can be larger than
        # the socket buffers, and the backend may already be writing output.
        writer = threading.Thread(
            target=self._pump_body, args=(proc.stdin, body, length), daemon=True
        )
        writer.start()
        try:
            status, headers = self._read_cgi_headers(proc.stdout)
            self._send_cgi_response(status, headers, proc.stdout)
        finally:
            writer.join(timeout=30)
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                returncode = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()
                returncode = proc.wait()
            stderr = proc.stderr.read() if proc.stderr else b""
            proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
            if body is not None:
                body.close()
            if returncode != 0:
                self.log_error(
                    "git-http-backend exited %s for %s: %s",
                    returncode,
                    path_info,
                    stderr.decode("utf-8", "replace").strip(),
                )

    def _pump_body(self, stdin, body, length: int) -> None:
        """Copy the request body into the CGI process, 64 KiB at a time."""
        source = body if body is not None else self.rfile
        try:
            remaining = length
            while remaining > 0:
                chunk = source.read(min(CHUNK, remaining))
                if not chunk:
                    break
                stdin.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    def _decode_chunked(self, dest) -> None:
        """Decode an HTTP/1.1 chunked request body into ``dest``."""
        while True:
            line = self.rfile.readline(1024)
            if not line:
                raise ConnectionError("truncated chunked body")
            size_field = line.split(b";", 1)[0].strip()
            if not size_field:
                continue
            size = int(size_field, 16)
            if size == 0:
                # Discard optional trailers up to the terminating blank line.
                while True:
                    trailer = self.rfile.readline(65536)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                return
            remaining = size
            while remaining > 0:
                data = self.rfile.read(min(CHUNK, remaining))
                if not data:
                    raise ConnectionError("truncated chunk data")
                dest.write(data)
                remaining -= len(data)
            dest.flush()
            self.rfile.read(2)  # trailing CRLF

    def _read_cgi_headers(self, stream):
        status = 200
        headers = []
        while True:
            line = stream.readline(65536)
            if not line or line in (b"\r\n", b"\n"):
                break
            name, _, value = line.decode("latin-1").partition(":")
            name, value = name.strip(), value.strip()
            if not name:
                continue
            if name.lower() == "status":
                try:
                    status = int(value.split()[0])
                except (ValueError, IndexError):
                    status = 200
            else:
                headers.append((name, value))
        return status, headers

    def _send_cgi_response(self, status: int, headers, stream) -> None:
        self.send_response(status)
        for name, value in headers:
            lowered = name.lower()
            if lowered in ("status", "connection", "transfer-encoding"):
                continue
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "HEAD":
            self.close_connection = True
            return
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            self.wfile.write(chunk)
        self.wfile.flush()
        self.close_connection = True

    # -- simple pages ---------------------------------------------------
    def _index(self) -> None:
        server = self.server
        repos = server.store.list()
        host = self.headers.get("Host") or "%s:%d" % server.server_address[:2]
        rows = []
        for repo in repos:
            name = html.escape(repo["name"])
            description = html.escape(repo["description"] or "no description")
            rows.append(
                '<li><a href="/%s.git"><strong>%s</strong></a> '
                "<span>%s</span><br><code>git clone http://%s/%s.git</code></li>"
                % (name, name, description, html.escape(host), name)
            )
        if not rows:
            rows.append(
                "<li>No repositories yet. Create one: "
                "<code>gituses create my-project</code></li>"
            )
        if not server.token:
            auth_note = "Anonymous access is enabled (no token configured)."
        else:
            auth_note = (
                "Authentication is required. Use the token as the password: "
                "<code>git clone http://git:TOKEN@%s/REPO.git</code>"
                % html.escape(host)
            )
        body = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>gituses - unlimited git</title>
<style>
body{font-family:system-ui,sans-serif;max-width:52rem;margin:3rem auto;padding:0 1rem;line-height:1.5;color:#1b1b1b}
code{background:#f3f3f3;padding:.1rem .3rem;border-radius:3px;word-break:break-all}
li{margin-bottom:1rem}
span{color:#666}
.tag{display:inline-block;background:#e7f5ea;color:#1d6b32;padding:.15rem .5rem;border-radius:999px;font-size:.8rem}
</style></head><body>
<h1>gituses <span class="tag">unlimited repos</span></h1>
<p>Self-hosted git over smart HTTP. No repository, user, push-size or
concurrency limits.</p>
<p>%s</p>
<ul>%s</ul>
<p>%d repositories &middot; gituses/%s</p>
</body></html>
""" % (
            auth_note,
            "\n".join(rows),
            len(repos),
            __version__,
        )
        self._plain(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _plain(self, status: int, body, content_type="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
            self.wfile.flush()
        self.close_connection = True

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
        if os.environ.get("GITUSES_QUIET"):
            return
        super().log_message(fmt, *args)


class GitusesServer(ThreadingHTTPServer):
    """Threaded HTTP server; unlimited concurrent connections and repos."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, address, store: Store, token: str | None = None):
        self.store = store
        self.token = store.token() if token is None else token
        super().__init__(address, GitusesHandler)


def serve(
    store: Store,
    host: str = "127.0.0.1",
    port: int = 8080,
    token: str | None = None,
) -> GitusesServer:
    """Create (but do not start) a server bound to ``host``/``port``."""
    return GitusesServer((host, port), store, token=token)

