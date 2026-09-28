"""
Package entry point — loads local configuration before anything reads it.
=========================================================================
`app/storage.py` and `app/db.py` read their settings at IMPORT time, so a .env
file has to be loaded before either of them is imported. This module is the only
place guaranteed to run first: importing `app.anything` imports `app` first.

A real environment always wins. Variables already set by the host — Railway,
Docker, your shell — are never overwritten by the file, so a deployment cannot
be quietly reconfigured by a .env that was committed by accident.

The file is a development convenience only. In deployment there is no .env;
the platform injects the real thing.
"""

from __future__ import annotations

import os
from pathlib import Path

# app/__init__.py -> parents[1] is webapp/backend, where the .env lives.
BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _load_env_file() -> None:
    """Read webapp/backend/.env, if it exists, without clobbering the real env."""
    env_path = Path(os.environ.get('CLEAR_ENV_FILE') or (BACKEND_ROOT / '.env'))
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        # python-dotenv is optional: without it the app still runs on real
        # environment variables, which is all a deployment ever uses.
        print(f'[env] {env_path.name} found but python-dotenv is not installed; '
              f'ignoring it')
        return
    # override=False is the important half: the host's own variables win.
    load_dotenv(env_path, override=False)


_load_env_file()
