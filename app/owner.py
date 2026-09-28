"""
Who a run belongs to — the single place that decides.
=====================================================
Every access rule lives here so there is exactly one predicate to tighten when
real authentication arrives, rather than a filter copied into a dozen queries.

TODAY
    The browser generates a UUID once and keeps it in localStorage, sending it
    as `X-Client-Id`. That is the owner. It is an IDENTIFIER, not a credential:
    anyone who knows one can use it, so it gates nothing of value on a
    single-user machine and must not be mistaken for a security boundary.

WHEN SIGN-IN LANDS
    `current_owner` starts returning the authenticated user id, and a user's
    first login CLAIMS every run still carrying their browser's client id
    (`claim_runs` below). Nobody's existing work is orphaned, and nothing else
    in the codebase changes — which is the entire reason this indirection
    exists before it is strictly needed.

LEGACY RUNS
    A run created before ownership existed has `owner_id IS NULL`. It is
    readable by anyone and is adopted by the first owner to touch it. That
    keeps every existing run and shared link working. Once auth is real, NULL
    should stop meaning "public": tighten `can_access` and drop the NULL arm.
"""

from __future__ import annotations

from fastapi import Header

# A browser that sends no client id at all - an old tab, a curl call, a health
# check. It gets its own bucket rather than being handed someone else's runs.
ANONYMOUS = 'anonymous'


async def current_owner(
    x_client_id: str | None = Header(default=None, alias='X-Client-Id'),
) -> str:
    """Resolve the caller to an owner id.

    A FastAPI dependency, so every endpoint that needs an owner declares it and
    none of them construct one. Swapping in real auth is a change to this
    function's body and nothing else.
    """
    value = (x_client_id or '').strip()
    # Bounded and stripped: it reaches a database column and a log line, and it
    # arrives from a header, so it is never trusted at the length the client
    # chose. A UUID is 36 characters; 64 leaves room for a user id later.
    if not value or len(value) > 64:
        return ANONYMOUS
    return value


def can_access(run, owner_id: str) -> bool:
    """Whether `owner_id` may see this run.

    NULL owner = created before ownership existed; readable by anyone until
    adopted. This is the one predicate to tighten when auth is real.
    """
    return run.owner_id is None or run.owner_id == owner_id
