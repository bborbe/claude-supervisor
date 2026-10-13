"""A fixture stand-in for the attention store's `session-heartbeat` routes.

`session-liveness.py` reads liveness from the store's HTTP API alone, so a suite that ran
against the real one would have a result that depends on the machine's fleet rather than on
the code — a live session on this host turns an ABSENT assertion into LIVE, the same trap the
heartbeat store already carries ("an isolated heartbeat store, and it is not optional"). This
starts a threaded HTTP server on an ephemeral port serving the two routes the reader uses, and
the suite points `--endpoint` (or `$ATTENTION_STORE_URL`) at it.

The routes mirror the store as measured 2026-10-08:

  GET /api/1.0/session-heartbeat          -> 200, a JSON array of rows (live and stale)
  GET /api/1.0/session-heartbeat/<id>     -> 200 with one row, or 404 `{"error": {...}}`

⚠️ **The per-id route is an EXACT lookup, deliberately** — that is the live store's behaviour,
and it is why a prefix has to fall back to the list route (measured: `/session-heartbeat/
30fae0ae` -> 404 for a live session). A fixture that prefix-matched here would hide that.
"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PATH = "/api/1.0/session-heartbeat"


def row(session_id, live=True, source="mcp-timer", state="", age_seconds=1):
    """One heartbeat row in the store's shape."""
    return {
        "session_id": session_id,
        "task": "",
        "vault": "",
        "location": "local",
        "state": state,
        "source": source,
        "at": "2026-10-08T00:00:00Z",
        "age_seconds": age_seconds,
        "live": live,
    }


def dead_url():
    """A URL on a port nothing is listening on — for the unreachable-endpoint cases.

    Bound and released so the number is almost certainly free, rather than a fixed port some
    other process on the host might hold.
    """
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "http://127.0.0.1:%d" % port


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):  # keep the suite's output clean
        pass

    def do_GET(self):
        store = self.server.store
        path = self.path.split("?", 1)[0]
        if path == PATH:
            self._send(store.list_status, store.rows)
            return
        if path.startswith(PATH + "/"):
            sid = path[len(PATH) + 1:]
            found = store.by_id.get(sid)
            if found is None:
                self._send(404, {"error": {"code": "NOT_FOUND", "message": "heartbeat not found"}})
            else:
                self._send(200, found)
            return
        self._send(404, {"error": {"code": "NOT_FOUND"}})

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FixtureEndpoint:
    """A running fixture store. Use as a context manager; `.url` is the base URL.

    `list_status` lets a case make the LIST route answer something other than 200 — the
    "non-200 that is not 404 is UNKNOWN" contract is driven through it.
    """

    def __init__(self, rows=None, list_status=200):
        self.rows = list(rows or [])
        self.list_status = list_status
        # ⚠️ Non-dict rows are skipped, not rejected: the reader must tolerate a malformed row
        # (a store that served one is not a reason to answer "no workers"), and a fixture that
        # raised here could not express that case at all.
        self.by_id = {
            r["session_id"]: r
            for r in self.rows
            if isinstance(r, dict) and r.get("session_id")
        }
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.store = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        host, port = self.server.server_address[:2]
        return "http://%s:%d" % (host, port)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
