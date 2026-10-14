"""A fixture stand-in for the attention store's `session-heartbeat` and `session-ledger` routes.

`session-liveness.py` reads liveness from the store's HTTP API alone, so a suite that ran
against the real one would have a result that depends on the machine's fleet rather than on
the code — a live session on this host turns an ABSENT assertion into LIVE, the same trap the
heartbeat store already carries ("an isolated heartbeat store, and it is not optional"). This
starts a threaded HTTP server on an ephemeral port serving the routes the readers use, and
the suite points `--endpoint` (or `$ATTENTION_STORE_URL`) at it.

The routes mirror the store as measured 2026-10-08 (heartbeat) and 2026-10-14 (ledger):

  GET /api/1.0/session-heartbeat          -> 200, a JSON array of rows (live and stale)
  GET /api/1.0/session-heartbeat/<id>     -> 200 with one row, or 404 `{"error": {...}}`
  GET /api/1.0/session-ledger             -> 200, a JSON array of rows
  GET /api/1.0/session-ledger/<id>        -> 200 with one row, or 404 `{"error": {...}}`

⚠️ **The per-id route is an EXACT lookup, deliberately** — that is the live store's behaviour,
and it is why a prefix has to fall back to the list route (measured: `/session-heartbeat/
30fae0ae` -> 404 for a live session). A fixture that prefix-matched here would hide that.

⚠️ **The ledger routes joined this fixture on 2026-10-14, and the reason is the same one the
heartbeat routes were here for.** The spawn ledger was a directory of files until then, so a
suite planted `<sid>.json` in a temp dir; `spawn-ledger.py` now reads the endpoint, and a
suite that still planted files would have every ledger assertion pass on a reader that saw
nothing — the empty-fleet read the whole change exists to prevent. `ledger_rows` is the
ledger's half of the same isolation, and `ledger_status` drives the non-200 contract the
reader turns into `None`.
"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PATH = "/api/1.0/session-heartbeat"
LEDGER_PATH = "/api/1.0/session-ledger"


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


def ledger_row(session_id, label="worker", **extra):
    """One spawn-ledger row in the store's wire shape.

    ⚠️ **The wire shape, not the old file record's.** The row carries every field the readers
    consume (`role`, `label`, `resumed_from`, `session_id`) plus the ones the server writes,
    and one field is RENAMED from the file's vocabulary: the file's `parent_session` is
    `spawner_session_id` here. A fixture that kept the old spelling would let a reader that
    still asks for `parent_session` — which gets `None` rather than an error — pass here and
    fail against the store.
    """
    record = {
        "session_id": session_id,
        "agent_id": "agent_1",
        "label": label,
        "mode": "interactive",
        "mode_source": "explicit",
        "launcher": "",
        "cwd": "",
        "worker_id": "",
        "target": "",
        "pane_id": "",
        "resumed_from": "",
        "policy": "",
        "spawner_session_id": "",
        "role": "agent",
        "task": "",
        "vault": "",
        "location": "local",
        "spawned_at": "2026-10-14T00:00:00Z",
        "status": "running",
        "unknown_reason": "",
        "result": None,
    }
    record.update(extra)
    return record


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
        if path == LEDGER_PATH:
            self._send(store.ledger_status, store.ledger_rows)
            return
        if path.startswith(LEDGER_PATH + "/"):
            sid = path[len(LEDGER_PATH) + 1:]
            found = store.ledger_by_id.get(sid)
            if found is None:
                self._send(404, {"error": {"code": "NOT_FOUND", "message": "session ledger entry not found"}})
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

    `list_status` lets a case make the heartbeat LIST route answer something other than 200 —
    the "non-200 that is not 404 is UNKNOWN" contract is driven through it — and `ledger_status`
    does the same for the ledger route, which is what the ledger reader turns into `None`.
    """

    def __init__(self, rows=None, list_status=200, ledger_rows=None, ledger_status=200):
        self.rows = list(rows or [])
        self.list_status = list_status
        self.ledger_rows = list(ledger_rows or [])
        self.ledger_status = ledger_status
        # ⚠️ Non-dict rows are skipped, not rejected: the reader must tolerate a malformed row
        # (a store that served one is not a reason to answer "no workers"), and a fixture that
        # raised here could not express that case at all.
        self.by_id = {
            r["session_id"]: r
            for r in self.rows
            if isinstance(r, dict) and r.get("session_id")
        }
        self.ledger_by_id = {
            r["session_id"]: r
            for r in self.ledger_rows
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
