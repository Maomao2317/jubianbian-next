"""Host-wide resource gates and provider admission, using a shared local volume.

OS file locks survive long requests and are released on process exit. There is
no expiring lease that can admit another request while a slow one is still alive.
Do not put this directory on an object store or a network filesystem.
"""
from __future__ import annotations

import hashlib
import os
import random
import sqlite3
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from .config import RESOURCE_DIR


def _try_lock(handle):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
    else:
        import fcntl
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


@contextmanager
def capacity(name: str, limit: int, *, directory: Path | None = None, timeout: float | None = None):
    directory = Path(directory or RESOURCE_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    prefix = hashlib.sha256(name.encode()).hexdigest()[:24]
    deadline = None if timeout is None else time.monotonic() + max(0, timeout)
    started = time.perf_counter()
    handles = []
    try:
        for index in range(max(1, limit)):
            handle = (directory / f"{prefix}-{index}.lock").open("a+b", buffering=0)
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
            handles.append(handle)
        while True:
            # Rotate the first slot to avoid repeatedly contending on slot zero.
            offset = random.randrange(len(handles))
            for index in range(len(handles)):
                handle = handles[(offset + index) % len(handles)]
                if _try_lock(handle):
                    try:
                        yield round((time.perf_counter() - started) * 1000, 1)
                    finally:
                        if os.name == "nt":
                            import msvcrt
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    return
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"Resource capacity wait exceeded: {name}")
            time.sleep(min(.1, max(0, deadline - time.monotonic())) if deadline is not None else .1)
    finally:
        for handle in handles:
            handle.close()


def limited(name: str, limit: int):
    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            with capacity(name, limit):
                return function(*args, **kwargs)
        return run
    return decorate


class ProviderBudget:
    """Shared rolling RPM/TPM reservations and 429 cooldowns.

Input token reservations are a configurable estimate, not a provider tokenizer.
Active calls keep their token reservation until they complete; actual usage then
replaces it for sixty seconds. Missing usage retains the conservative estimate.
"""
    def __init__(self, directory: Path | None = None):
        self.directory = Path(directory or RESOURCE_DIR)

    @contextmanager
    def _db(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.directory / "provider-budget.sqlite3", timeout=10)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, scope TEXT, started REAL, tokens INTEGER, expires REAL)")
            connection.execute("CREATE TABLE IF NOT EXISTS cooldowns (scope TEXT PRIMARY KEY, until REAL)")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reserve(self, scope: str, *, rpm: int = 0, tpm: int = 0, tokens: int = 0, lifetime: float = 1800, timeout: float = 600) -> str:
        if tpm and tokens > tpm:
            raise ValueError("TPM quota is smaller than one request's token reservation")
        identity = uuid.uuid4().hex
        deadline = time.monotonic() + timeout
        while True:
            now = time.time()
            with self._db() as connection:
                connection.execute("DELETE FROM calls WHERE expires <= ?", (now,))
                connection.execute("DELETE FROM cooldowns WHERE until <= ?", (now,))
                blocked = connection.execute("SELECT until FROM cooldowns WHERE scope = ?", (scope,)).fetchone()
                count, used = connection.execute(
                    "SELECT SUM(CASE WHEN started > ? THEN 1 ELSE 0 END), COALESCE(SUM(tokens), 0) FROM calls WHERE scope = ?",
                    (now - 60, scope),
                ).fetchone()
                if not blocked and (not rpm or (count or 0) < rpm) and (not tpm or used + tokens <= tpm):
                    connection.execute("INSERT INTO calls VALUES (?, ?, ?, ?, ?)", (identity, scope, now, tokens, now + lifetime))
                    return identity
            if time.monotonic() >= deadline:
                raise TimeoutError("Provider quota wait exceeded")
            time.sleep(.25)

    def wait_cooldown(self, scope: str, timeout: float = 300):
        deadline = time.monotonic() + timeout
        while True:
            with self._db() as connection:
                row = connection.execute("SELECT until FROM cooldowns WHERE scope = ?", (scope,)).fetchone()
            remaining = (row[0] - time.time()) if row else 0
            if remaining <= 0:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Provider cooldown wait exceeded")
            time.sleep(min(.25, remaining))

    def finish(self, identity: str, tokens: int | None = None):
        with self._db() as connection:
            connection.execute("UPDATE calls SET tokens = COALESCE(?, tokens), expires = ? WHERE id = ?", (tokens, time.time() + 60, identity))

    def cooldown(self, scope: str, seconds: float):
        with self._db() as connection:
            connection.execute(
                "INSERT INTO cooldowns VALUES (?, ?) ON CONFLICT(scope) DO UPDATE SET until = MAX(until, excluded.until)",
                (scope, time.time() + max(0, min(300, seconds))),
            )


provider_budget = ProviderBudget()
