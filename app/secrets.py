"""
Handing a credential to a worker without putting it in the queue.
=================================================================
One value has this problem today: the user's Google Gemini key, typed into the
upload form and used only by the BOQ parser, for spreadsheet columns the rules
and the fuzzy matcher could not resolve.

WHY IT CANNOT BE A TASK ARGUMENT
    Celery serialises arguments into the broker. They sit in the queued message
    until the task runs, they are repeated in retries, and they are printed in
    full inside failure tracebacks — which is where a credential is most likely
    to be read by someone who should not have it. `store.create` and
    `jobs/parse.py` have said from the beginning that this key is deliberately
    never persisted; making it a task argument would quietly end that.

WHAT HAPPENS INSTEAD
    The key is written under its own broker entry with a short expiry, and the
    task reads it ONCE and deletes it. It is still in Redis, briefly — the
    difference is that it is there deliberately, for seconds rather than for the
    life of a queue, and it never appears in a traceback.

    With no broker there is nothing to store: the value is handed straight to
    the thread, exactly as before.

WHEN ACCOUNTS ARRIVE
    This becomes a per-user setting held server-side, and the whole module goes
    away. It exists to stop a temporary arrangement from becoming a permanent
    leak.
"""

from __future__ import annotations

from app.celery_app import ENABLED, REDIS_URL

# Long enough for a queued parse to start on a busy worker, short enough that a
# forgotten value is not sitting there tomorrow.
TTL_SECONDS = 30 * 60

_PREFIX = 'clear:secret:'


def _client():
    import redis
    return redis.Redis.from_url(REDIS_URL)


def stash(run_id: str, value: str | None) -> bool:
    """Hold a secret for one run's task. Returns whether there is one to fetch.

    False for an empty value, so callers do not have to special-case "the user
    left the field blank" - much the commonest case.
    """
    if not value:
        return False
    if not ENABLED:
        # No broker: the caller passes the value directly to the thread.
        return False
    try:
        _client().setex(f'{_PREFIX}{run_id}', TTL_SECONDS, value)
        return True
    except Exception as exc:
        # A missing key degrades the BOQ parser to its rule and fuzzy matchers,
        # which is how it behaves for everyone who does not supply one. Not
        # worth failing an upload over.
        print(f'[secrets] could not stash a secret for {run_id}: {exc}')
        return False


def take(run_id: str) -> str | None:
    """Read a stashed secret and delete it. One task, one read."""
    if not ENABLED:
        return None
    try:
        client = _client()
        key = f'{_PREFIX}{run_id}'
        value = client.get(key)
        client.delete(key)
        return value.decode() if value else None
    except Exception as exc:
        print(f'[secrets] could not read the secret for {run_id}: {exc}')
        return None


def discard(run_id: str) -> None:
    """Drop a stashed secret whose task will never run — a failed upload."""
    if not ENABLED:
        return
    try:
        _client().delete(f'{_PREFIX}{run_id}')
    except Exception:
        pass
