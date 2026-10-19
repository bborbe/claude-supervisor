#!/usr/bin/env python3
"""Drive the a2a-worker-controller's CONTROL call against the LOCAL backend, from a slash command.

    a2a-local-control.py create --task "<name>" [--vault <name>] [--first-step <text>]
                                                  # start one worker on the local backend
    a2a-local-control.py list                     # every worker the controller owns

The controller exposes one JSON-RPC endpoint carrying three operations (`create` / `list` /
`stop`), and this file carries the two a slash command needs on the local backend, which its
launchd wrapper serves at `http://localhost:8092`. It is the CONTROL half only: it starts a worker
and reports the fleet, and never speaks to a worker itself.

⚠️ THE WIRE SHAPE IS `server/a2a-client.mjs`'s, MIRRORED RATHER THAN RE-DERIVED. That module is the
shipped client and this file exists only because a shell command cannot import it; a second reading
of the controller's schema is exactly the drift the plugin's single-home rule forbids. Four rules
it pins, each of which yields a request that LOOKS right and is not:

  1. THE PART IS `{"data": ...}`, NEVER `{"text": ...}`. The controller reads the `ControlRequest`
     from the message's FIRST data part and answers `ErrInvalidParams` to a message carrying none.
     A text part is not a near miss — no operation runs at all.
  2. THE DATA PAYLOAD MUST BE A NON-NULL OBJECT. The SDK hydrates `data` into a `*any`, so a JSON
     `null` leaves that pointer nil and counts as no discriminator, which the SDK refuses.
  3. THE CALL IS ONE-SHOT — the bare method `SendMessage`, and NO `accept: text/event-stream`. A
     one-shot route answers a single object; asking it for a stream reads as a call that produced
     no frames.
  4. EXACTLY ONE OPERATION-SPECIFIC FIELD. The controller refuses a request that sets none or more
     than one instead of guessing the intent, so `create` carries `{create: {task}}` and `list`
     carries neither.

⚠️ A CONTROLLER-REPORTED FAILURE IS NOT "NOTHING HAPPENED". `TASK_STATE_FAILED` arrives with no
artifact and no detail, and the failure can land in the controller's task store AFTER the executor
has run — so a `create` on that path can have started a worker and still read FAILED. It gets its
own exit code and its own sentence, because a caller must reconcile it against `list` rather than
retry blindly.

⚠️ THE TOKEN IS RESOLVED AT RUN TIME AND NEVER PRINTED. It comes from TeamVault under the key
`mcp-start.py` uses for `SUPERVISOR_A2A_TOKEN_DOCKER` (the local controller's own control secret),
or from `$A2A_AUTH_TOKEN` when the controller's wrapper has already exported it. Only the KEY NAME
is ever named; the value reaches no stream, no error string and no line of this file. The `create`
reply carries a per-worker token beside its record, so the printer redacts any token-shaped key
rather than echoing it.

Exit codes — one per failure class, so a caller can branch without parsing a sentence:

    0  OK                 the call ran and its reply was read
    1  TRANSPORT          the controller could not be reached, or answered a non-200 status
    2  CONTROLLER_FAILED  the reply was TASK_STATE_FAILED — outcome UNKNOWN, reconcile via `list`
    3  UNREADABLE         the reply was not a readable ControlResponse for the operation sent
    4  NO_TOKEN           no control token could be resolved
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# The local backend's endpoint: `--endpoint`, then `$SUPERVISOR_A2A_URL_LOCAL`, then localhost.
#
# ⚠️ THE URL CARRIES THE `/a2a` PATH, and the default below is the whole endpoint rather than an
# origin. The controller mounts its JSON-RPC handler at `pkg/a2a.go`'s `A2APath` (`/a2a`), so a POST
# to the bare origin answers `404 page not found` — measured 2026-10-17 on the first live SC3 probe,
# where every local row failed until `/a2a` was appended. ⚠️ **Do not "fix" a caller-supplied URL by
# appending the path here**: every sibling spells the endpoint in full (`a2a-spawn.mjs` documents
# `http://127.0.0.1:8080/a2a`, and the k8s builder returns `"https://" + host + "/a2a"`), so an
# append would turn a correct `…/a2a` into `…/a2a/a2a`. The shape is the caller's contract; only the
# default was wrong. `scripts/tests/test_a2a_local_control.py` pins it.
#
# ⚠️ THE LOCAL BACKEND GETS ITS OWN VARIABLE, and reusing the `docker` one would be a live bug
# rather than a shortcut: the two controllers are separate deployments on THIS Mac — the local one
# on :8092, the docker one on :8090 — so `SUPERVISOR_A2A_URL_DOCKER` names a DIFFERENT service, and
# reading it here would send a local create to the docker backend the moment it is set. `config.mjs`
# keeps the same split under `a2a.url` (`{docker, k8s}`), which is why no `local` key exists there.
ENDPOINT_DEFAULT = "http://localhost:8092/a2a"
ENDPOINT_ENV = "SUPERVISOR_A2A_URL_LOCAL"

# The token: the wrapper's own exported name first, then the TeamVault key `mcp-start.py` resolves
# the docker controller's secret from. ⚠️ A lookup key is a short identifier rather than a secret;
# the VALUE it names is never logged, printed or embedded anywhere in this file.
TOKEN_ENV = "A2A_AUTH_TOKEN"
TOKEN_KEY = "pLv8Mw"

SEND_METHOD = "SendMessage"
USER_ROLE = "ROLE_USER"
STATE_FAILED = "TASK_STATE_FAILED"

# A control call can start a container, so it is given longer than a liveness probe's ten seconds.
TIMEOUT_SECONDS = 30

# Verdicts, in the order the docstring lists them.
OK = 0
TRANSPORT = 1
CONTROLLER_FAILED = 2
UNREADABLE = 3
NO_TOKEN = 4

# A reply body that opens a line with `data:` is SSE; the one-shot framing is bare JSON.
SSE_FRAME = re.compile(r"^\s*data:", re.MULTILINE)

# A key whose name looks like a credential. The `create` reply carries the worker's own token, and
# a printer that echoed it verbatim would put a credential on stdout and into the caller's
# transcript — so the projection drops it rather than trusting a filter to catch it later.
SECRET_KEY = re.compile(r"token|secret|password", re.IGNORECASE)


def endpoint_for(explicit: str | None) -> str:
    """The controller's base URL, resolved the way every other reader in this plugin resolves one."""
    if explicit:
        return explicit.rstrip("/")
    env = os.environ.get(ENDPOINT_ENV)
    if env:
        return env.rstrip("/")
    return ENDPOINT_DEFAULT


def teamvault_config(env):
    """The teamvault-cli config path, following teamvault-cli's own precedence."""
    explicit = env.get("SUPERVISOR_TEAMVAULT_CONFIG") or env.get("TEAMVAULT_CONFIG")
    if explicit:
        return explicit
    home = env.get("HOME") or str(Path.home())
    return str(Path(home) / ".config" / "teamvault-cli" / "config.json")


def teamvault_password(key, env, run=subprocess.run):
    """The value for `key`, or None. ⚠️ The VALUE is never logged — only the key is."""
    cli = env.get("SUPERVISOR_TEAMVAULT_CLI") or "teamvault-cli"
    argv = [
        cli,
        "password",
        f"--teamvault-config={teamvault_config(env)}",
        f"--teamvault-key={key}",
    ]
    try:
        result = run(argv, capture_output=True, text=True)
    except OSError as error:
        print(f"a2a-local-control: cannot run {cli} for key {key}: {error}", file=sys.stderr)
        return None
    if result.returncode != 0:
        # teamvault-cli's own stderr names the problem and carries no secret; stdout is the value,
        # and it is not echoed even on the success path.
        print(
            f"a2a-local-control: teamvault-cli failed for key {key}: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    value = result.stdout.strip()
    if not value:
        print(f"a2a-local-control: teamvault-cli returned an empty value for key {key}", file=sys.stderr)
        return None
    return value


def resolve_token(env, lookup=teamvault_password):
    """The control token, from the environment then TeamVault, or None when neither yields one."""
    explicit = env.get(TOKEN_ENV)
    if explicit:
        return explicit
    return lookup(TOKEN_KEY, env)


def control_request(operation, task=None, vault=None, first_step=None):
    """The `ControlRequest` for one operation, or None when the shape the controller requires is unmet.

    ⚠️ Exactly one operation-specific field, which is the controller's own rule rather than this
    file's preference: it refuses a request that sets none or more than one instead of guessing.
    """
    if operation == "list":
        return {"operation": "list"}
    if operation == "create" and isinstance(task, str) and task != "":
        # `vault` / `first_step` are optional and omitted when empty, matching the controller's
        # `omitempty`: an empty vault means the backend's own configured vault.
        create = {"task": task}
        if vault:
            create["vault"] = vault
        if first_step:
            create["first_step"] = first_step
        return {"operation": "create", "create": create}
    return None


def send_request_for(endpoint, token, request):
    """The one-shot JSON-RPC request — `{url, headers, body}` — mirroring `a2aSendRequestFor`.

    ⚠️ No `accept: text/event-stream`: a one-shot route answers a single object, and asking it for
    a stream reads as a call that produced no frames. `contextId` is sent for the same reason the
    worker half sends it — a control call is a turn on the controller's conversation.
    """
    return {
        "url": endpoint,
        "headers": {
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
        },
        "body": {
            "jsonrpc": "2.0",
            "id": 1,
            "method": SEND_METHOD,
            "params": {
                "message": {
                    "messageId": str(uuid.uuid4()),
                    "role": USER_ROLE,
                    "parts": [{"data": request}],
                    "contextId": str(uuid.uuid4()),
                }
            },
        },
    }


def post(url, headers, body, timeout=TIMEOUT_SECONDS):
    """`(status, text)` from the controller, or `(None, reason)` when it could not be reached.

    ⚠️ A non-200 is returned rather than raised, and is the caller's TRANSPORT class: the call
    failed at the HTTP layer, before any JSON-RPC, exactly as a connection that never opened did.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        # Reached it, and it refused at the HTTP layer — read the body rather than letting the
        # exception carry the status out of the process as a traceback.
        return error.code, error.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as error:
        return None, str(error)


def event_state(update):
    """The state a `statusUpdate` frame carries — `{status: {state}}`, with the flat form accepted.

    ⚠️ It is NESTED in a2a-go v2's `TaskStatusUpdateEvent`; reading it one level too shallow finds
    no status and files a completed turn as whatever the task frame said first.
    """
    if not isinstance(update, dict):
        return None
    status = update.get("status")
    if isinstance(status, dict) and status.get("state"):
        return status["state"]
    return update.get("state") or None


def control_reply(text):
    """`(task, frames, error)` from either framing — a one-shot body or SSE.

    Mirrors `a2aControlReply`. ⚠️ The result is a `SendMessageResponse` ONEOF — `{task: …}` or
    `{message: …}` — NOT the task itself; a client that read `result` as the task found no status
    and no artifact on a `create` that had in fact started a worker. A bare task is still accepted,
    so a server that does not wrap keeps working. Both framings are genuinely served, so both are
    accepted; the one-shot shape is the one this file asks for.
    """
    if SSE_FRAME.search(text):
        frames = []
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            raw = line[len("data:") :].strip()
            if raw == "" or raw == "[DONE]":
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return None, None, f"an SSE frame is not JSON: {raw[:200]}"
            if isinstance(payload, dict) and payload.get("error"):
                return None, None, f"the controller refused the control call: {json.dumps(payload['error'])}"
            frames.append(payload.get("result", payload) if isinstance(payload, dict) else payload)
        candidates = [one.get("task", one) if isinstance(one, dict) else one for one in frames]
        task = next((one for one in candidates if isinstance(one, dict) and one.get("id")), {})
        return task, frames, None

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, None, f"the control reply is neither SSE nor JSON: {text[:200]}"
    if isinstance(payload, dict) and payload.get("error"):
        # A refusal here is a JSON-RPC error, not a FAILED task — the request never reached a backend.
        return None, None, f"the controller refused the control call: {json.dumps(payload['error'])}"
    result = payload.get("result", payload) if isinstance(payload, dict) else payload
    task = (result.get("task") or result.get("message") or result) if isinstance(result, dict) else result
    return task, [task], None


def control_response(task, frames, sent_operation):
    """`(response, error, controller_failed)` for a control reply.

    ⚠️ THE STATE IS READ BEFORE THE PAYLOAD, and the order is load-bearing: a backend failure yields
    `TASK_STATE_FAILED` with NO artifact, so a client that hunted for the data part first would find
    none and have to invent a reason, while the status states the real one.

    `sent_operation` is asserted against the operation that comes back, because a reply naming a
    different operation is the only client-side observable that the answer does not belong to the
    call that asked for it.
    """
    states = [event_state(one.get("statusUpdate")) for one in frames if isinstance(one, dict)]
    states = [state for state in states if state]
    task_status = task.get("status") if isinstance(task, dict) else None
    state = states[-1] if states else (task_status.get("state") if isinstance(task_status, dict) else None)

    if state == STATE_FAILED:
        return None, (
            "the controller reported the control call FAILED and returns no detail on this path. "
            "⚠️ It does NOT mean the operation did not happen — the failure can land in the "
            "controller's task store AFTER the executor ran — so treat the outcome as UNKNOWN and "
            "reconcile against `list`."
        ), True

    artifacts = task.get("artifacts") if isinstance(task, dict) else None
    parts = list(artifacts) if isinstance(artifacts, list) else []
    for one in frames:
        if not isinstance(one, dict):
            continue
        update = one.get("artifactUpdate")
        artifact = (update.get("artifact") if isinstance(update, dict) else None) or one.get("artifact")
        if isinstance(artifact, dict):
            parts.append(artifact)

    data_parts = [
        part["data"]
        for artifact in parts
        if isinstance(artifact, dict)
        for part in (artifact.get("parts") or [])
        if isinstance(part, dict) and "data" in part
    ]
    response = next((one for one in data_parts if one is not None), None)
    if response is None:
        return None, (
            f"the control call ended {json.dumps(state) if state else 'with no status'} but carried "
            "no data part, so there is no ControlResponse to read. ⚠️ A text part here is not a near "
            "miss: the controller answers with a data part."
        ), False
    if isinstance(response, dict) and response.get("operation") != sent_operation:
        return None, (
            f"the control call asked for {json.dumps(sent_operation)} but the reply names "
            f"{json.dumps(response.get('operation'))} — the answer does not belong to this call."
        ), False
    return response, None, False


def redact(value):
    """`value` with any token-shaped key replaced — the projection every printed reply goes through.

    The `create` reply carries the worker's own token beside its record, so a printer that echoed
    the reply verbatim would put a credential on stdout and into the caller's transcript.
    """
    if isinstance(value, dict):
        return {
            key: ("<redacted>" if isinstance(key, str) and SECRET_KEY.search(key) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def bind_line(bind):
    """One line naming the create's session-bind outcome, mirroring `spawn_agent`'s `bind`.

    ⚠️ `none` (absent/null) is NOT a failure: no session registered within the controller's bind
    window, so nothing was bound yet — read `list` for the session id later. `error` means a
    session was found and the vault write failed.
    """
    if not isinstance(bind, dict):
        return "bind: none"
    if bind.get("error"):
        return f"bind: error {bind['error']}"
    if bind.get("action") in ("stamped", "appended"):
        return f"bind: {bind['action']}"
    return f"bind: unreadable {json.dumps(redact(bind))}"


def print_created(response):
    """Print the created worker — its id first, then the reply — and say whether it was readable."""
    created = response.get("create") if isinstance(response, dict) else None
    worker = created.get("worker") if isinstance(created, dict) else None
    worker_id = worker.get("worker_id") if isinstance(worker, dict) else None
    if not isinstance(worker_id, str) or worker_id == "":
        print(f"the create reply carried no worker_id: {json.dumps(redact(response))}", file=sys.stderr)
        return False
    print(f"worker_id: {worker_id}")
    session_id = worker.get("session_id")
    if isinstance(session_id, str) and session_id:
        print(f"session_id: {session_id}")
    print(bind_line(created.get("bind")))
    print(json.dumps(redact(response), indent=2, sort_keys=True))
    return True


def print_workers(response):
    """Print one worker per line — its id, and the session id when the controller reports one."""
    listing = response.get("list") if isinstance(response, dict) else None
    workers = listing.get("workers") if isinstance(listing, dict) else None
    # ⚠️ `null` and a missing key are the same fact here — no workers — because the controller's
    # store copies a task through gob, which does not transmit zero values, so an empty list
    # round-trips as `{"workers": null}`. Normalised rather than returned as-is: `null` is not
    # iterable, and every caller's first move is to iterate.
    if workers is None:
        workers = []
    if not isinstance(workers, list):
        print(f"`list.workers` must be an array or null, got {json.dumps(workers)}", file=sys.stderr)
        return False
    for worker in workers:
        worker_id = worker.get("worker_id") if isinstance(worker, dict) else None
        session_id = worker.get("session_id") if isinstance(worker, dict) else None
        line = str(worker_id)
        if isinstance(session_id, str) and session_id:
            line = f"{line}  session_id={session_id}"
        print(line)
    return True


def main(argv=None) -> int:
    # ⚠️ `--endpoint` is declared on BOTH the root parser and each subparser so it parses on either
    # side of the subcommand. The subparser copies default to SUPPRESS, never to a value: a plain
    # `default=None` there overwrites a value already parsed on the root side, so `--endpoint X
    # list` would silently fall back to the default — the classic argparse subparser gotcha, and
    # why the flag is spelled twice rather than shared through a `parents=[…]` container.
    endpoint_help = f"the controller's JSON-RPC endpoint, path included (default {ENDPOINT_DEFAULT})"
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", default=None, help=endpoint_help)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="start one worker on the local backend")
    create.add_argument("--task", required=True, metavar="NAME", help="the task the worker is opened for")
    create.add_argument("--vault", default=None, metavar="NAME", help="the vault-cli vault the task lives in")
    create.add_argument("--first-step", default=None, metavar="TEXT", help="text told to the worker ahead of its work prompt")
    create.add_argument("--endpoint", default=argparse.SUPPRESS, help=endpoint_help)
    list_workers = commands.add_parser("list", help="print every worker the controller owns")
    list_workers.add_argument("--endpoint", default=argparse.SUPPRESS, help=endpoint_help)

    args = parser.parse_args(argv)

    request = control_request(
        args.command,
        getattr(args, "task", None),
        getattr(args, "vault", None),
        getattr(args, "first_step", None),
    )
    if request is None:
        parser.error(f"`{args.command}` requires a non-empty --task name")

    token = resolve_token(os.environ)
    if not token:
        print(
            f"a2a-local-control: no control token — set ${TOKEN_ENV}, or make TeamVault key "
            f"{TOKEN_KEY} readable",
            file=sys.stderr,
        )
        return NO_TOKEN

    call = send_request_for(endpoint_for(args.endpoint), token, request)
    status, text = post(call["url"], call["headers"], call["body"])
    if status is None:
        print(f"a2a-local-control: the controller could not be reached: {text}", file=sys.stderr)
        return TRANSPORT
    if status != 200:
        print(f"a2a-local-control: the controller answered HTTP {status}: {text[:200]}", file=sys.stderr)
        return TRANSPORT

    task, frames, error = control_reply(text)
    if error:
        print(f"a2a-local-control: {error}", file=sys.stderr)
        return UNREADABLE

    response, error, controller_failed = control_response(task, frames, args.command)
    if controller_failed:
        print(f"a2a-local-control: {error}", file=sys.stderr)
        return CONTROLLER_FAILED
    if error:
        print(f"a2a-local-control: {error}", file=sys.stderr)
        return UNREADABLE

    printer = print_created if args.command == "create" else print_workers
    if not printer(response):
        return UNREADABLE
    return OK


if __name__ == "__main__":
    sys.exit(main())
