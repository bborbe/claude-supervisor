#!/usr/bin/env python3
"""Bearer-token handling shared by the attention store's local readers.

Every local reader beside this file talks to the same store over the same URL, so
the token read and the `Authorization` header live here once rather than being
hand-copied into each of them. Read it as a sibling module, the way the other
shared scripts in this directory are read:

    _load("attention_store_auth", "attention-store-auth.py")

⚠️ `pod-attention.py` deliberately does NOT load this. It is deployed inside a
pod, where a sibling file may not exist, so it carries its own copy of the same
two ideas. Keep the two in step by hand; that duplication is the price of the
script having to run alone.

⚠️ This is a credential path. The token is never logged, never rendered, and
never placed in an error message — `headers()` is the only thing here that
touches it, and it puts the value in exactly one place.
"""
import os

# The shared secret the store's second, cluster-reachable listener requires.
#
# ⚠️ Empty is the legitimate local case, not a misconfiguration. The store's
# second listener is disabled when its own token is empty, and the loopback
# listener is unauthenticated by design — so an unset token here sends no header
# and the request still succeeds against loopback. That is why this is a plain
# read with a default rather than a hard failure.
TOKEN = os.environ.get("ATTENTION_STORE_TOKEN", "")


def headers(extra=None):
    """Request headers, carrying the bearer token only when one is set.

    `extra` is merged underneath, so a caller keeps its own Content-Type or
    Accept and gains the Authorization header when a token exists. Nothing is
    sent when the token is empty — an empty `Bearer ` is not a credential and
    a store that enforces would refuse it anyway.
    """
    merged = dict(extra) if extra else {}
    if TOKEN:
        merged["Authorization"] = f"Bearer {TOKEN}"
    return merged


def request(url, data=None, extra=None, method=None):
    """A urllib Request carrying this module's headers.

    The bare-`urlopen(url)` call sites in the readers have no headers at all
    today; routing them through here is what gives them the token without each
    one re-deriving the merge.
    """
    import urllib.request

    return urllib.request.Request(
        url,
        data=data,
        headers=headers(extra),
        method=method,
    )
