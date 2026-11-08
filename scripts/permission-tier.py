#!/usr/bin/env python3
"""Classify a parked tool call into one of three permission tiers.

    allowlist — a server/manager-allowlist.json entry, answered without judgment.
    manager   — everything else a worker does: writes, git reads, cluster reads.
    operator  — the operator-only class; always reaches the operator, never a manager.

The ruling and the manager's rubric live in docs/permission-allowlist.md; this is the machine half.

⚠️ GIT READS ARE NOT READS. `git status` runs `core.fsmonitor`; `core.pager`, `diff.external`,
textconv drivers and hooks do the same, and the shipped policy lets a worker write `.git/config`
(or the `.git` gitdir pointer in a linked worktree). So no git subcommand is ever `allowlist`
here — it is `manager` at best. See README § Allowing a Bash command.

⚠️ EVERY SEGMENT IS CHECKED, AND WHAT CANNOT BE SPLIT IS `operator`. Split on `;`, `&&`, `||`, `|`,
`&`, newline and subshell parens outside quotes, recursing into `$(…)`, backticks, `<(…)`, `sh -c`
and shell-fed heredocs. A shape it cannot fully take apart — an unterminated quote or heredoc (a
truncated payload ends in one), an expansion as the command word, `eval`, `source` — is `operator`,
never `manager`. An over-eager manager tier is worse than a park: it hides a real gate.

⚠️ OPERATOR-ONLY WINS OVER THE ALLOWLIST: `cat` is an allowlist entry and `cat ~/.netrc` matches it, so the secret form is checked first — same order as the doc's deny list.

Pure: no filesystem, environment or clock — the caller passes the entries and the context.
"""

import json
import os
import re
import shlex
import sys

TIERS = ("allowlist", "manager", "operator")

# A payload of this length was cut by the attention store's card cap, so whatever it described is
# not fully visible and cannot be verified.
PAYLOAD_CAP = 2000


class Unverifiable(Exception):
    """The command cannot be fully taken apart, so it cannot be shown to be outside the class."""


# --------------------------------------------------------------------------------------------
# Lexing: one pass over the text, quote-aware, collecting segments and nested command texts.
# --------------------------------------------------------------------------------------------

_HEREDOC = re.compile(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


def _read_balanced(s, i, open_ch, close_ch):
    """(inner, index after close) for a group whose opener sits just before s[i]."""
    depth = 1
    j = i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "'":
            k = s.find("'", j + 1)
            if k < 0:
                raise Unverifiable("unterminated single quote inside substitution")
            j = k + 1
            continue
        if c == '"':
            j = _skip_double(s, j + 1)
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return s[i:j], j + 1
        j += 1
    raise Unverifiable(f"unterminated {open_ch}{close_ch} group")


def _skip_double(s, j):
    """Index just after the double quote closing the one that opened before s[j]."""
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == '"':
            return j + 1
        if s.startswith("$(", j):
            _, j = _read_balanced(s, j + 2, "(", ")")
            continue
        if c == "`":
            j = _find_backtick(s, j + 1) + 1
            continue
        j += 1
    raise Unverifiable("unterminated double quote")


def _find_backtick(s, j):
    while j < len(s):
        if s[j] == "\\":
            j += 2
            continue
        if s[j] == "`":
            return j
        j += 1
    raise Unverifiable("unterminated backtick")


def _nested_in_double(s, start, end, nested):
    """Collect `$(…)` and backtick bodies inside the double-quoted span s[start:end]."""
    j = start
    while j < end:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if s.startswith("$((", j):
            _, j = _read_balanced(s, j + 3, "(", ")")
            continue
        if s.startswith("$(", j):
            inner, j = _read_balanced(s, j + 2, "(", ")")
            nested.append(inner)
            continue
        if c == "`":
            k = _find_backtick(s, j + 1)
            nested.append(s[j + 1:k])
            j = k + 1
            continue
        j += 1


def _is_redirect_amp(s, i):
    """`2>&1`, `&>file`, `>&2` — an ampersand that belongs to a redirection, not a separator."""
    return (i > 0 and s[i - 1] in "<>") or (i + 1 < len(s) and s[i + 1] == ">")


def lex(cmd):
    """Split `cmd` into (segments, nested, heredocs).

    segments — simple-command strings, substitutions replaced by the word `__SUBST__`
    nested   — command texts found in `$(…)`, backticks, `<(…)` / `>(…)`
    heredocs — (body, quoted, segment_index) for every heredoc
    Raises Unverifiable on any shape it cannot take apart.
    """
    segments, nested, heredocs = [], [], []
    buf = []
    pending = []  # (delimiter, strip_tabs, quoted, segment_index) awaiting the next newline
    i, n = 0, len(cmd)
    at_word_start = True

    def end_segment():
        text = "".join(buf).strip()
        if text:
            segments.append(text)
        buf.clear()

    while i < n:
        c = cmd[i]
        if c == "\\":
            if i + 1 < n and cmd[i + 1] == "\n":
                i += 2  # line continuation
                continue
            buf.append(cmd[i:i + 2])
            i += 2
            at_word_start = False
            continue
        if cmd.startswith(("$'", '$"'), i):
            # ANSI-C / locale quoting: `git $'push' --force` is `git push --force` to bash but
            # `$push` to shlex, so the subcommand check would never see it.
            raise Unverifiable("ANSI-C or locale quoting ($'…' / $\"…\")")
        if c == "'":
            k = cmd.find("'", i + 1)
            if k < 0:
                raise Unverifiable("unterminated single quote")
            buf.append(cmd[i:k + 1])
            i = k + 1
            at_word_start = False
            continue
        if c == '"':
            k = _skip_double(cmd, i + 1)
            _nested_in_double(cmd, i + 1, k - 1, nested)
            buf.append(cmd[i:k])
            i = k
            at_word_start = False
            continue
        if c == "#" and at_word_start:
            k = cmd.find("\n", i)
            i = n if k < 0 else k
            continue
        if cmd.startswith("$((", i):
            inner, i = _read_balanced(cmd, i + 3, "(", ")")
            _nested_in_double(inner, 0, len(inner), nested)  # arithmetic can embed $(…)/`…`
            buf.append("$__SUBST__")
            at_word_start = False
            continue
        if cmd.startswith(("$(", "<(", ">("), i):
            inner, i = _read_balanced(cmd, i + 2, "(", ")")
            nested.append(inner)
            buf.append("$__SUBST__")
            at_word_start = False
            continue
        if c == "`":
            k = _find_backtick(cmd, i + 1)
            nested.append(cmd[i + 1:k])
            buf.append("$__SUBST__")
            i = k + 1
            at_word_start = False
            continue
        if cmd.startswith("<<<", i):
            buf.append("<<< ")
            i += 3
            continue
        m = _HEREDOC.match(cmd, i)
        if m:
            pending.append((m.group(3), m.group(1) == "-", bool(m.group(2)), len(segments)))
            buf.append(" ")
            i = m.end()
            continue
        if c == "\n":
            end_segment()
            i += 1
            at_word_start = True
            for delim, strip_tabs, quoted, seg_index in pending:
                body = []
                while True:
                    if i >= n:
                        raise Unverifiable(f"unterminated heredoc {delim}")
                    k = cmd.find("\n", i)
                    line = cmd[i:] if k < 0 else cmd[i:k]
                    i = n if k < 0 else k + 1
                    probe = line.lstrip("\t") if strip_tabs else line
                    if probe == delim:
                        break
                    body.append(line)
                heredocs.append(("\n".join(body), quoted, seg_index))
            pending = []
            continue
        if c in ";|()" or (c == "&" and not _is_redirect_amp(cmd, i)):
            end_segment()
            i += 2 if cmd.startswith(("&&", "||", "|&", ";;"), i) else 1
            at_word_start = True
            continue
        buf.append(c)
        at_word_start = c in " \t"
        i += 1
    if pending:
        raise Unverifiable(f"unterminated heredoc {pending[0][0]}")
    end_segment()
    return segments, nested, heredocs


# --------------------------------------------------------------------------------------------
# Words: shell-split one segment, strip what only changes HOW a program runs.
# --------------------------------------------------------------------------------------------

_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECT_TOKEN = re.compile(r"^\d*(>>?|<|&>>?|>&)\S*$")
_PASSTHROUGH = {"command", "builtin", "nohup", "time", "exec", "nice", "caffeinate", "timeout"}
_KEYWORDS = {"if", "then", "elif", "else", "do", "while", "until", "!", "{", "}"}
_NOOP_HEADS = {"for", "case", "esac", "fi", "done", "in", "select", "function", "}", "]]", "[["}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_WRAPPERS = _PASSTHROUGH | {"env", "xargs", "stdbuf", "ionice", "chrt", "taskset", "watch",
                            "parallel", "flock", "unbuffer", "script", "nsenter", "setsid", "strace",
                            "ltrace", "chroot", "unshare", "bwrap", "firejail", "timeout", "env"}


def words(segment):
    """(env, argv) for one segment, or raise Unverifiable."""
    env, toks, _ = segment_words(segment)
    return env, toks


def segment_words(segment):
    """(env, argv, writes) for one segment — `writes` are output-redirect targets."""
    try:
        toks = shlex.split(segment, comments=False, posix=True)
    except ValueError as err:
        raise Unverifiable(f"cannot split segment: {err}")
    # A redirect is dropped as an operator but its TARGET is kept as an argument AND recorded
    # as a write, so the path checks see it: `echo k >~/.ssh/authorized_keys` must not lose
    # `~/.ssh/` with the `>`, and `echo x > ~/.claude/settings.json` is a write, not a read.
    kept, writes, next_is_write = [], [], False
    for t in toks:
        if next_is_write:
            next_is_write = False
            kept.append(t)
            writes.append(t)
            continue
        if ">" in t and not _REDIRECT_TOKEN.match(t):  # `x>~/f` is one shlex word: `x` + a redirect
            kept.append(t[:t.index(">")]); t = t[t.index(">"):]
        m = _REDIRECT_TOKEN.match(t)
        if not m:
            kept.append(t)
            continue
        op, target = m.group(1), t[m.end(1):].lstrip("&")
        is_output = op != "<"
        if not target:
            next_is_write = is_output
            continue
        if not re.fullmatch(r"\d+|-", target):
            kept.append(target)
            if is_output:
                writes.append(target)
    toks = kept
    while toks and toks[0] in _KEYWORDS:
        toks = toks[1:]
    env = {}
    while toks and _ENV.match(toks[0]):
        k, _, v = toks[0].partition("=")
        env[k] = v
        toks = toks[1:]
    while toks and toks[0] in _PASSTHROUGH:
        toks = toks[1:]
        while toks and (toks[0].startswith("-") or re.fullmatch(r"\d+[smhd]?", toks[0])):
            toks = toks[1:]  # `timeout 30 …`, `nice -n 5 …`
    if toks and toks[0] == "env" and len(toks) > 1:
        toks = toks[1:]
        while toks and (_ENV.match(toks[0]) or toks[0].startswith("-")):
            if _ENV.match(toks[0]):
                k, _, v = toks[0].partition("=")
                env[k] = v
            toks = toks[1:]
        if not toks:
            toks = ["env"]  # `env -i` with no command: keep the word so `_secret_cmd` sees a dump
    return env, toks, writes


# --------------------------------------------------------------------------------------------
# The operator-only class. Each check takes (env, argv) and returns a form name or None.
# --------------------------------------------------------------------------------------------

_PROD_PATH = re.compile(r"(^|[/~])[\w.-]*-prod(/|$)")
_PROD_WRAPPERS = {"kubectlprod", "kubectlnukeprod", "kubectlsmallprod"}
_DEV_WRAPPERS = {"kubectlnukedev", "kubectldev", "kubectlsmalldev", "kubectlquant", "kubectlhell",
                 "kubectlsun", "kubectlstaging"}
_SECRET_PATH = re.compile(
    r"(^|[/~\s=])(\.ssh/|\.netrc\b|\.aws/|\.config/gh\b|\.kube/config\b|\.env(\.[\w-]+)?$|"
    r"id_(rsa|ed25519|ecdsa)\b|\.npmrc\b|\.git-credentials\b|\.pgpass\b|\.pypirc\b|\.docker/config\.json\b|\.gnupg/|\.vault-token\b|application_default_credentials\.json|\.(bash|zsh|sh)_history\b|^/proc/|/proc/\d+/environ)")
_PROTECTED_REF = re.compile(r"^(?:[^:]*:)?(refs/heads/)?(master|main)$")


_SECRET_FILES = ("~/.netrc", "~/.pgpass", "~/.git-credentials", "~/.ssh/id_rsa", "~/.ssh/id_ed25519", "~/.ssh/id_ecdsa",
                 "~/.aws/credentials", "~/.kube/config", "~/.docker/config.json", "~/.npmrc", ".env", "~/.pypirc",
                 "~/.vault-token", "~/.config/gh/hosts.yml", "~/.config/gcloud/application_default_credentials.json")


def _glob_hits_secret(arg):
    """True when a glob (`~/.netr?`, `~/.ss*/id_*`) could expand to a known credential file."""
    m = re.search(r"[*?\[]", arg)
    if not m or len(arg[:m.start()]) < 3:  # `*` and `*.json` match everything; not a secret
        return False
    rx = re.sub(r"\\\[((?:[^\\\[\]]|\\.)+?)\\\]", r"[\1]", re.escape(arg).replace(r"\*", ".*").replace(r"\?", "."))
    try:
        return any(re.fullmatch(rx, f) for f in _SECRET_FILES)
    except re.error:  # an unbalanced `[` (the `[ -f x ]` test word) is literal to the shell too
        return False


def _prod(env, argv):
    if env.get("BRANCH") == "prod" or "BRANCH=prod" in argv:
        return "prod"
    if any(re.search(r"=prod(uction)?$", x) for x in argv) \
            or any(argv[i] == "prod" and argv[i - 1].startswith("-") for i in range(1, len(argv))):
        return "prod"  # -chdir=prod / --context=prod, and the space-separated spelling
    if any(_PROD_PATH.search(a) for a in argv):
        return "prod"
    if argv and os.path.basename(argv[0]) in _PROD_WRAPPERS:
        return "prod"
    return None


def _git_sub(argv):
    """(subcommand, args) for a git call, skipping global options; None when not git."""
    if not argv or os.path.basename(argv[0]) != "git":
        return None
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") else 1
    if i >= len(argv):
        return None
    return argv[i], argv[i + 1:]


def _git(env, argv):
    g = _git_sub(argv)
    if g is None:
        return None
    sub, args = g
    # An expansion where the decision is made (`git $SUB`, `git push $FLAGS origin x`) can be
    # any subcommand or flag at run time.
    if "$" in sub or (sub in ("push", "branch", "tag", "reset", "clean", "update-ref", "stash")
                      and any("$" in a for a in args)):
        raise Unverifiable(f"git {sub}: an expansion decides what runs")
    if sub == "push":
        flags = [a for a in args if a.startswith("-")]
        refs = [a for a in args if not a.startswith("-")]
        if any(re.fullmatch(r"-[A-Za-z]*[fd][A-Za-z]*", f) or f in ("--mirror", "--prune", "--all") or f.startswith(("--force", "--delete"))
               for f in flags):
            return "force-push/ref-delete"
        if "--tags" in flags or "--follow-tags" in flags:
            return "publish"
        refspecs = refs[1:]
        if not refspecs:
            return "push-unverifiable-target"  # bare push: the target is whatever upstream is set
        for r in refspecs:
            if r.startswith(("+", ":")):
                return "force-push/ref-delete"
            if _PROTECTED_REF.match(r) or r == "HEAD":
                return "merge-to-protected"
            if r.startswith("refs/tags/") or re.match(r"^v\d", r.split(":")[-1]):
                return "publish"
        return None
    if sub == "branch" and any(re.fullmatch(r"-[A-Za-z]*[dD][A-Za-z]*", a) or a == "--delete" for a in args):  # -Dr, -df
        return "ref-delete"
    if sub == "tag" and any(re.fullmatch(r"-[A-Za-z]*d[A-Za-z]*", a) or a == "--delete" for a in args):
        return "ref-delete"
    if sub == "update-ref" and "-d" in args:
        return "ref-delete"
    if sub == "reset" and "--hard" in args:
        return "irreversible-delete"
    if sub in ("clean", "filter-branch", "filter-repo"):
        return "irreversible-delete"
    if sub == "stash" and args[:1] and args[0] in ("drop", "clear"):
        return "irreversible-delete"
    if sub == "worktree" and args[:1] == ["remove"] and ("--force" in args or "-f" in args):
        return "irreversible-delete"
    if sub == "config" and any(a in ("--global", "--system") for a in args):
        return "permission-grant"
    return None


def _gh(env, argv):
    if not argv or os.path.basename(argv[0]) != "gh":
        return None
    a = argv[1:]
    while a and a[0] in ("-R", "--repo"):
        a = a[2:]
    a = [x for x in a if not x.startswith("--repo=")]
    if any("$" in x for x in a[:2]):
        raise Unverifiable("gh: an expansion decides the subcommand")
    if a[:2] == ["pr", "merge"]:
        return "merge-to-protected"
    if a[:2] == ["workflow", "run"]:
        return "publish"  # dispatches a workflow, which may deploy
    if a[:1] == ["release"] and a[1:2] and a[1] in ("create", "edit", "delete", "upload"):
        return "publish"
    if a[:2] in (["pr", "comment"], ["issue", "create"], ["issue", "comment"], ["pr", "review"]):
        return "publish"
    if a[:2] == ["auth", "token"] or a[:1] == ["secret"]:
        return "secret"
    if a[:1] == ["repo"] and a[1:2] and a[1] in ("delete", "archive", "edit", "rename"):
        return "irreversible-delete"
    if a[:1] == ["api"]:
        method = None
        for i, x in enumerate(a):
            if x in ("-X", "--method") and i + 1 < len(a):
                method = a[i + 1].upper()
            elif x.startswith("--method="):
                method = x.split("=", 1)[1].upper()
        fields = any(x in ("-f", "-F", "--field", "--raw-field", "--input") for x in a)
        if method in ("POST", "PUT", "PATCH", "DELETE") or (fields and method != "GET"):
            return "publish"
    return None


def _kubectl(env, argv):
    if not argv:
        return None
    name = os.path.basename(argv[0])
    if not name.startswith("kubectl"):
        return None
    if name in _PROD_WRAPPERS:
        return "prod"
    if name not in _DEV_WRAPPERS:
        return "cluster-unknown-env"  # bare kubectl or an unknown wrapper: environment unknowable
    a = argv[1:]
    values = {i + 1 for i, x in enumerate(a)
              if x in ("-n", "--namespace", "-o", "--output", "-l", "--selector", "-c", "--container")}
    if any(a[i] == "prod" for i in values if i < len(a) and a[i - 1] in ("-n", "--namespace")) \
            or any(x in ("--namespace=prod", "-n=prod") for x in a):
        return "prod"
    positional = [x for i, x in enumerate(a) if i not in values and not x.startswith("-")]
    if positional and "$" in positional[0] or any(a[i].startswith("$") for i in values if i < len(a)):
        raise Unverifiable("kubectl: an expansion decides the verb or namespace")
    verb = positional[0] if positional else ""
    if verb in ("delete", "drain", "cordon", "taint", "replace", "edit", "patch", "create",
                "exec", "port-forward", "cp", "attach", "debug"):
        return "cluster-mutation"
    if verb == "apply" and not any(x.startswith("--dry-run") for x in a):
        return "cluster-mutation"
    if verb == "get":
        if any(v in ("secret", "secrets") or v.startswith("secret/") for v in positional[1:]):
            return "secret"
        outputs = [a[i] for i in values if i < len(a) and a[i - 1] in ("-o", "--output")]
        outputs += [x.split("=", 1)[1] if "=" in x else x[2:] for x in a
                    if x.startswith(("-o=", "--output=")) or re.match(r"^-o(yaml|json)", x)]
        if any(o.lstrip("=") in ("yaml", "json") for o in outputs):
            return "secret"  # last-applied-configuration embeds the object, credentials included
    return None


def _files(env, argv):
    if not argv:
        return None
    name = os.path.basename(argv[0])
    rest = argv[1:]
    if name == "rm":
        targets = [x for x in rest if not x.startswith("-")]
        if not targets or not all(t.startswith(("/tmp/", "/private/tmp/")) and ".." not in t
                                  for t in targets):
            return "irreversible-delete"
        return None
    if name in ("shred", "srm", "truncate", "dd", "mkfs", "diskutil"):
        return "irreversible-delete"
    if name == "find" and "-delete" in rest:
        return "irreversible-delete"
    if any(_SECRET_PATH.search(x) or _glob_hits_secret(x) for x in argv):
        return "secret"
    return None


_SECRET_CMDS = {"teamvault", "teamvault-cli", "op", "pass", "vault", "aws-vault", "printenv"}


def _secret_cmd(env, argv):
    if not argv:
        return None
    name = os.path.basename(argv[0])
    if name in _SECRET_CMDS:
        return "secret"
    if name == "security" and any(x.startswith(("find-", "dump-", "export")) for x in argv[1:]):
        return "secret"
    # `env`/`env -i`/`env -u X` with NO command operand dumps the environment; options are not
    # a command. A real operand is a wrapper, handled in `words()`.
    if name == "env" and not [a for a in argv[1:] if not a.startswith("-") and not _ENV.match(a)]:
        return "secret"
    return None


def _localhost(args):
    return any(re.match(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?(/|$)", x) for x in args)


def _publish(env, argv):
    if not argv:
        return None
    name = os.path.basename(argv[0])
    a = argv[1:]
    if name == "gog":
        if any(x in ("send", "reply", "react") for x in a):
            return "publish"
        if a[:2] == ["auth", "tokens"]:
            return "secret"
        return None
    if name in ("npm", "pnpm", "yarn", "cargo", "twine") and "publish" in a:
        return "publish"
    if name == "goreleaser":
        return "publish"
    if name == "docker" and any(x in ("push", "login") for x in a):
        return "publish"  # the verb may follow a global flag (`docker --context prod push`)
    if name == "docker" and (a[:1] and a[0] in ("rm", "rmi", "prune")
                             or a[1:2] and a[1] in ("rm", "prune")):
        return "irreversible-delete"  # `docker rm`, `docker volume rm`, `docker system prune`
    if name in ("curl", "http", "wget"):
        if any(x.startswith("$") for x in a):
            raise Unverifiable(f"{name}: an expansion may carry the method or body")
        if _localhost(a):
            return None
        writes = ("POST", "PUT", "PATCH", "DELETE")
        for i, x in enumerate(a):
            if x in ("-X", "--request", "--method") and i + 1 < len(a) and a[i + 1].upper() in writes:
                return "publish"
            if re.match(r"^(-X|--request=|--method=)(POST|PUT|PATCH|DELETE)$", x, re.IGNORECASE):
                return "publish"
            # Any body-carrying flag makes the call a write: `-d`, every `--data*`, `-F`/`--form*`,
            # `--json`, `-T`/`--upload-file`, wget's `--post-data` / `--post-file` / `--body-*`.
            if x in ("-d", "-F", "-T") or x.startswith(("--data", "--form", "--json", "--upload-file",
                                                         "--post-", "--body-")):
                return "publish"
        return None
    if name in ("ssh", "scp", "rsync", "sftp"):
        return "remote-host"
    return None


def _trade(env, argv):
    if argv and re.search(r"\btrade-(create|close|modify)\b|trading-api-write", " ".join(argv)):
        return "live-trade"
    return None


def _deploy(env, argv):
    if not argv:
        return None
    name = os.path.basename(argv[0])
    a = argv[1:]
    if name == "make":
        targets = [x for x in a if not x.startswith("-") and "=" not in x]
        if any(t in ("buca", "apply", "deploy", "upload", "release") for t in targets):
            branch = env.get("BRANCH") or next((x.split("=", 1)[1] for x in a if x.startswith("BRANCH=")), None)
            if branch not in ("dev", "develop"):
                return "prod"  # an unstated environment is prod
        return None
    if name in ("launchctl", "systemctl", "brew") and a[:1] and a[0] in (
            "kickstart", "load", "unload", "bootout", "bootstrap", "restart", "stop", "start", "services"):
        if name == "launchctl" and a[0] == "kickstart" and a[-1:] and a[-1].endswith("/com.bborbe.attention-watcher"):
            return None  # the one documented no-ask restart
        return "service-restart"
    if name in ("helm", "terraform", "tofu") and any(x in (
            "install", "upgrade", "uninstall", "delete", "apply", "destroy", "import") for x in a):
        return "prod"  # the verb may follow a global flag (`terraform -chdir=prod apply`)
    if name in ("sudo", "doas", "su"):
        return "privilege"
    if name in ("kill", "pkill", "killall"):
        return "service-restart"  # stops a running process; a pattern can hit a sibling's
    return None


_CHECKS = (_prod, _deploy, _git, _gh, _kubectl, _trade, _publish, _secret_cmd, _files)

# Literal forms scanned in text the splitter does not run as shell (heredoc data, interpreter
# code). A hit there is treated as the form itself — conservative by design: a PR body that QUOTES
# `gh pr merge` escalates, and that is the cheap direction to be wrong in.
_TEXT_FORMS = (
    ("force-push/ref-delete", re.compile(r"git\s+push\b[^\n]*\s(--force\b|-f\b|--delete\b|--mirror\b)")),
    ("merge-to-protected", re.compile(r"gh\s+(-R\s+\S+\s+)?pr\s+merge\b")),
    ("prod", re.compile(r"BRANCH=prod\b|kubectl(nuke|small)?prod\b|nuke-deploy\s+prod\b")),
    ("secret", re.compile(r"\.netrc\b|\.ssh/|id_rsa\b|\.aws/credentials|security\s+find-\w*-password")),
    ("publish", re.compile(r"gog\s+(gmail|chat)\b[^\n]*\bsend\b|gh\s+release\s+create\b|npm\s+publish\b|docker\s+push\b")),
    ("live-trade", re.compile(r"\btrade-(create|close|modify)\b|trading-api-write")),
)

_INTERPRETERS = {"python", "python3", "node", "bun", "deno", "ruby", "perl", "osascript"}


def _scan_text(text):
    for form, rx in _TEXT_FORMS:
        if rx.search(text):
            return form
    return None


def _check_argv(env, argv):
    for check in _CHECKS:
        form = check(env, argv)
        if form:
            return form
    return None


def _inline_program_index(argv, letters, long_flags=()):
    """Index of the inline-program argument after `-c` (or a combined `-lc`, `-ec`, `-Bc`), or None.

    A combined short-flag cluster counts when its LAST letter is one of `letters`: `bash -lc X`
    and `python3 -Bc X` take X as the program, exactly as `-c X` does. Missing this sent
    `bash -lc 'git push --force …'` to the manager tier unread.
    """
    for i, a in enumerate(argv[1:], start=1):
        if a in long_flags:
            return i + 1
        if re.fullmatch(r"-[A-Za-z]+", a) and a[-1] in letters:
            return i + 1
    # No early stop at the first operand: option VALUES (`-o pipefail`, `-X importtime`) look
    # like operands, and stopping there sent `bash -o pipefail -c '…'` to manager unread. A
    # `-c` after a real script operand is read as inline too — the conservative direction.
    return None


def _reads_program_from_stdin(argv):
    """A shell or ssh invocation whose program comes from stdin: no inline program, no script."""
    if not argv:
        return False
    base = os.path.basename(argv[0])
    if base == "ssh":
        return len([a for a in argv[1:] if not a.startswith("-")]) <= 1  # host only, no command
    if base not in _SHELLS:
        return False
    if _inline_program_index(argv, "c") is not None:
        return False
    operands = [a for a in argv[1:] if not a.startswith("-")]
    return not operands or "-s" in argv


def classify_bash(cmd, depth=0):
    """The first operator form found anywhere in `cmd`, or None. Raises Unverifiable."""
    if depth > 8:
        raise Unverifiable("substitution nesting too deep")
    if "<<<" in cmd:  # a here-string feeds stdin (`bash <<< 'git push --force …'`)
        raise Unverifiable("here-string feeds stdin this check cannot see")
    segments, nested, heredocs = lex(cmd)
    for inner in nested:
        form = classify_bash(inner, depth + 1)
        if form:
            return form
    # A shell (or ssh) that reads its program from stdin — no `-c`, no script file — runs
    # whatever is piped or redirected into it. Every heredoc in the command may be that
    # program (`cat <<EOF | bash`), so all of them are split as shell. With no heredoc the
    # input is unseen (`curl … | sh`), which is unverifiable.
    stdin_shell = any(_reads_program_from_stdin(words(s)[1]) for s in segments)
    if stdin_shell and not heredocs:
        raise Unverifiable("a shell reads its program from stdin this check cannot see")
    for body, quoted, seg_index in heredocs:
        form = _scan_text(body)
        if form:
            return form
        if stdin_shell:
            form = classify_bash(body, depth + 1)
            if form:
                return form
            continue
        if not quoted:  # unquoted: expands `$(…)` AND backticks before the program reads it
            expansions = []
            _nested_in_double(body, 0, len(body), expansions)
            for inner in expansions:
                form = classify_bash(inner, depth + 1)
                if form:
                    return form
    for seg in segments:
        env, argv, writes = segment_words(seg)
        targets = list(writes)
        if argv and (os.path.basename(argv[0]) in _WRITE_CMDS
                     or (os.path.basename(argv[0]) == "sed" and any(a.startswith("-i") for a in argv))):
            targets += [a for a in argv[1:] if not a.startswith("-")]
        for t in targets:
            if _SETTINGS.search(t):
                return "permission-grant"
            if _SECRET_PATH.search(t):
                return "secret"
        if not argv:
            if any(v in ("prod",) for k, v in env.items() if k == "BRANCH"):
                return "prod"
            continue
        if any(re.search(r"\{[^}]*,[^}]*\}", a) for a in argv):
            raise Unverifiable("brace expansion hides the real argument")  # cat ~/{.netrc,x}
        head = argv[0]
        if head in _NOOP_HEADS:  # `for f in ~/.netrc` — the operands still feed the loop body
            form = _prod(env, argv[1:]) or ("secret" if any(_SECRET_PATH.search(a) for a in argv[1:]) else None)
            if form:
                return form
            continue
        if "$" in head or "__SUBST__" in head:  # `git${IFS}push` hides the program mid-word
            raise Unverifiable(f"command word is an expansion: {head!r}")
        if head in ("eval", "source", "."):
            raise Unverifiable(f"{head} runs text this check cannot see")
        base = os.path.basename(head)
        if base in _SHELLS:
            i = _inline_program_index(argv, "c")
            if i is not None:
                if i >= len(argv):
                    raise Unverifiable("sh -c with no command")
                form = classify_bash(argv[i], depth + 1)
                if form:
                    return form
                continue
        if base in _INTERPRETERS:
            i = _inline_program_index(argv, "ce", long_flags=("--eval",))
            if i is not None and i < len(argv):
                form = _scan_text(argv[i])
                if form:
                    return form
        if base == "xargs":
            rest = [x for x in argv[1:] if not x.startswith("-")]
            if rest:
                form = _check_argv(env, rest)
                if form:
                    return form
        # A wrapper runs the program named somewhere in its arguments, and an option VALUE
        # (`timeout --signal KILL 5 rm …`, `env -u VAR rm …`) can sit where `words()` expects the
        # program. So every suffix is checked: the real program is found wherever it starts.
        # `find`'s action flags (`-exec`, `-execdir`, `-ok`, `-okdir`) are the same shape.
        raw = shlex.split(seg)
        heads = [os.path.basename(t) for t in raw if not _ENV.match(t)][:1]
        if base == "find" or (heads and heads[0] in _WRAPPERS):
            for i in range(1, len(raw)):
                form = _check_argv(env, raw[i:])
                if not form and os.path.basename(raw[i]) in _SHELLS:
                    j = _inline_program_index(raw[i:], "c")
                    if j is not None and j < len(raw) - i:
                        form = classify_bash(raw[i + j], depth + 1)
                if form:
                    return form
        form = _check_argv(env, argv)
        if form:
            return form
    return None


# --------------------------------------------------------------------------------------------
# Non-Bash tools.
# --------------------------------------------------------------------------------------------

# The harness's own permission and execution surface: settings, hooks and installed plugins.
# Writing any of them changes what runs next, and how — a permission grant by another name.
# Matched with or without a leading directory, so a relative `.claude/settings.json` counts.
_SETTINGS = re.compile(
    r"(^|/)\.claude/(settings(\.local)?\.json$|hooks/|plugins/)|(^|/)\.claude\.json$")
_WRITE_CMDS = {"cp", "mv", "tee", "uniq", "ln", "install", "rsync", "chmod", "chown", "truncate", "touch"}


def classify_tool(tool, tool_input):
    """Operator form for a non-Bash tool call, or None."""
    inp = tool_input if isinstance(tool_input, dict) else {}
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Read"):
        path = str(inp.get("file_path") or inp.get("notebook_path") or "")
        if _SECRET_PATH.search(path):
            return "secret"
        if tool != "Read" and _SETTINGS.search(path):
            return "permission-grant"
        if tool != "Read" and _PROD_PATH.search(path):
            return "prod"
        return None
    if tool == "Skill":
        name = str(inp.get("skill") or "")
        args = str(inp.get("args") or "")
        if name.split(":")[-1] in ("nuke-deploy", "make-buca") and re.search(r"\bprod\b", args):
            return "prod"
        if name.split(":")[-1] == "github-release":
            return "publish"
        return None
    if tool.startswith("mcp__"):
        low = tool.lower()
        if "trading-prod" in low and re.search(r"(update|trigger|create|close|replay)", low):
            return "live-trade"
        if re.search(r"(send|post|reply|publish|merge|push|delete|transition|addcomment|create(issue|page|comment))", low):
            return "publish"
        # The NAME says what a tool is for; the INPUT says what it touches — same forms as a redirect.
        for v in inp.values():
            if isinstance(v, str) and (_SETTINGS.search(v) or _SECRET_PATH.search(v)):
                return "permission-grant" if _SETTINGS.search(v) else "secret"
    return None


# --------------------------------------------------------------------------------------------
# Allowlist: the same whole-token prefix match server/manager-allowlist.mjs performs.
# --------------------------------------------------------------------------------------------

_SHELL_META = re.compile(r"[;&|`<>\n(){}*?\[\]$'\"\\]")  # + globs/expansion/quoting: the shell rewrites them
_NON_SHELL_WS = re.compile(r"[^\S \t]")


def command_tokens(cmd):
    if not isinstance(cmd, str) or _SHELL_META.search(cmd) or _NON_SHELL_WS.search(cmd):
        return None
    toks = [t for t in re.split(r"[ \t]+", cmd.strip()) if t]
    if not toks or _ENV.match(toks[0]):
        return None
    return toks


def match_allowlist(entries, tool, cmd, context=None):
    context = context or {}
    toks = command_tokens(cmd)
    if not entries or toks is None:
        return None
    for e in sorted(entries, key=lambda e: -len(e["prefix"].split())):
        if e.get("tool") not in ("*", tool):
            continue
        prefix = e["prefix"].split()
        if not prefix or len(prefix) > len(toks):
            continue
        if e.get("exact") and len(toks) != len(prefix):
            continue
        if toks[:len(prefix)] != prefix:
            continue
        confine = e.get("confine")
        if confine is not None and (confine != "clean-runner-config"
                                    or context.get("runnerConfigClean") is not True):
            continue  # an unknown confine is one nothing enforces: fail closed, never allowlist
        return e
    return None


def load_allowlist(path=None):
    path = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "server", "manager-allowlist.json")
    try:
        with open(path, encoding="utf-8") as fh:
            entries = json.load(fh).get("entries")
        return entries if isinstance(entries, list) else []
    except (OSError, ValueError, AttributeError):
        return []  # fail closed: no allowlist means nothing is `allowlist`


# --------------------------------------------------------------------------------------------
# Entry points.
# --------------------------------------------------------------------------------------------

def tier_for(tool, tool_input, allowlist_entries=None, context=None):
    """{tier, form, reason} for one parked call."""
    if tool == "Bash":
        cmd = tool_input.get("command") if isinstance(tool_input, dict) else tool_input
        if not isinstance(cmd, str) or not cmd.strip():
            return {"tier": "operator", "form": "unverifiable", "reason": "no command text"}
        try:
            form = classify_bash(cmd)
        except Unverifiable as err:
            return {"tier": "operator", "form": "unverifiable", "reason": str(err)}
        if form:
            return {"tier": "operator", "form": form, "reason": f"operator-only form: {form}"}
        entry = match_allowlist(allowlist_entries or [], tool, cmd, context)
        if entry:
            return {"tier": "allowlist", "form": None, "reason": f"allowlist entry `{entry['prefix']}`"}
        return {"tier": "manager", "form": None, "reason": "no operator-only form in any segment"}
    form = classify_tool(tool, tool_input)
    if form:
        return {"tier": "operator", "form": form, "reason": f"operator-only form: {form}"}
    return {"tier": "manager", "form": None, "reason": f"{tool}: no operator-only form"}


def parse_payload(payload):
    """(tool, input) from an attention-card payload `Tool: text`, or (None, None)."""
    if not isinstance(payload, str) or ":" not in payload:
        return None, None
    tool, _, rest = payload.partition(":")
    tool, rest = tool.strip(), rest.lstrip(" ")
    if not re.fullmatch(r"[A-Za-z_][\w-]*", tool):
        return None, None
    if tool == "Bash":
        return tool, {"command": rest}
    if tool in ("Edit", "Write", "Read", "MultiEdit", "NotebookEdit"):
        return tool, {"file_path": rest.split("\n", 1)[0].strip()}
    if tool == "Skill":
        name, _, args = rest.partition(" ")
        return tool, {"skill": name.lstrip("/"), "args": args}
    return tool, {"raw": rest}


def tier_for_payload(payload, allowlist_entries=None):
    """Classify a stored card payload. A payload cut at the store's cap is `operator`."""
    if isinstance(payload, str) and len(payload) >= PAYLOAD_CAP:
        return {"tier": "operator", "form": "unverifiable", "reason": "payload truncated at the store cap"}
    tool, inp = parse_payload(payload)
    if tool is None:
        return {"tier": "operator", "form": "unverifiable", "reason": "payload names no tool"}
    return tier_for(tool, inp, allowlist_entries)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Classify a parked tool call into a permission tier.")
    ap.add_argument("--tool")
    ap.add_argument("--input", help="Bash command text, or a JSON object for other tools")
    ap.add_argument("--payload", help="an attention-card payload, `Tool: text`")
    ap.add_argument("--runner-config-clean", action="store_true",
                    help="the caller checked the runner config is unmodified (confined test entries)")
    ns = ap.parse_args(argv)
    if ns.input == "-":  # the wrapper sends large inputs on stdin, where argv would hit E2BIG
        ns.input = sys.stdin.read()
    context = {"runnerConfigClean": True} if ns.runner_config_clean else {}
    entries = load_allowlist()
    if ns.payload is not None:
        out = tier_for_payload(ns.payload, entries)
    elif ns.tool:
        if ns.tool == "Bash":
            inp = {"command": ns.input or ""}
        else:
            try:
                inp = json.loads(ns.input or "{}")
            except ValueError:
                inp = {"file_path": ns.input}
        out = tier_for(ns.tool, inp, entries, context)
    else:
        ap.error("pass --payload, or --tool with --input")
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
