"""Cross-process GPU cooldowns and single recovery probes; never persist secrets."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import logging
import sqlite3
import time
import uuid
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener, ProxyHandler

from . import config


class CircuitOpen(RuntimeError):
    pass


class RemoteCircuit:
    def __init__(self, directory=None):
        self.directory = directory

    @contextmanager
    def _db(self):
        directory = self.directory or config.RESOURCE_DIR
        directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(directory / "gpu-circuit.sqlite3", timeout=2)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS circuits (scope TEXT PRIMARY KEY, epoch TEXT, state TEXT, until REAL, failures INTEGER)")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def admit(self, scope, timeout):
        now = time.time()
        with self._db() as connection:
            row = connection.execute("SELECT epoch,state,until,failures FROM circuits WHERE scope=?", (scope,)).fetchone()
            if row and row[1] == "closed":
                return row[0], False
            if row and now < row[2]:
                raise CircuitOpen("GPU temporarily unavailable; use CPU fallback")
            epoch = uuid.uuid4().hex
            connection.execute("INSERT INTO circuits VALUES (?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET epoch=excluded.epoch,state=excluded.state,until=excluded.until",
                               (scope, epoch, "probe", now + timeout + 10, row[3] if row else 0))
            return epoch, True

    def finish(self, scope, epoch, success):
        with self._db() as connection:
            row = connection.execute("SELECT failures FROM circuits WHERE scope=? AND epoch=?", (scope, epoch)).fetchone()
            if row is None:
                return  # An older in-flight success cannot undo a newer failure.
            failures = 0 if success else row[0] + 1
            delay = min(300, config.GPU_CIRCUIT_COOLDOWN_SECONDS * 2 ** min(failures - 1, 5)) if failures else 0
            connection.execute("UPDATE circuits SET epoch=?,state=?,until=?,failures=? WHERE scope=? AND epoch=?",
                               (epoch if success else uuid.uuid4().hex, "closed" if success else "open",
                                time.time() + delay, failures, scope, epoch))


circuit = RemoteCircuit()
logger = logging.getLogger("jubianbian")


def _finish(scope, epoch, success):
    try:
        circuit.finish(scope, epoch, success)
    except (sqlite3.Error, OSError):
        # Bookkeeping must not discard a valid response or mask the original
        # remote error. Admission still fails over to CPU if storage is broken.
        logger.warning("gpu_circuit_state_update_failed success=%s", success)


def _scope(kind, url, token):
    parts = urlsplit(url)
    # Both Docker networks route to the same local GPU tunnel.
    host = "host-gateway" if parts.hostname in {"172.18.0.1", "172.19.0.1"} else parts.hostname
    endpoint = urlunsplit((parts.scheme, f"{host}:{parts.port or (443 if parts.scheme == 'https' else 80)}", parts.path.rstrip('/'), '', ''))
    return hashlib.sha256(f"{kind}\0{endpoint}\0{token}".encode()).hexdigest()


def _probe(kind, url, token):
    request = Request(url.rstrip('/') + "/health", headers={"Authorization": "Bearer " + token})
    with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
        raw = response.read(16385)
    if len(raw) > 16384:
        raise ValueError("Invalid GPU health response")
    value = json.loads(raw)
    ready = value.get("asrReady") if kind == "asr" else value.get("ready")
    if ready is not True:
        raise CircuitOpen("GPU not ready; use CPU fallback")


@contextmanager
def remote_attempt(kind, url, token, timeout):
    scope = _scope(kind, url, token)
    epoch, probe = circuit.admit(scope, timeout + 3)
    try:
        if probe:
            _probe(kind, url, token)
        yield
    except Exception:
        _finish(scope, epoch, False)
        raise
    else:
        _finish(scope, epoch, True)
