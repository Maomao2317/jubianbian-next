import tempfile
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app import remote_circuit as remote


class RemoteCircuitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gate = remote.RemoteCircuit(Path(self.temp.name))

    def test_two_instances_share_cooldown_and_allow_only_one_recovery_probe(self):
        other = remote.RemoteCircuit(Path(self.temp.name))
        with patch.object(remote.time, "time", return_value=100):
            epoch, probe = self.gate.admit("gpu", 60)
            self.assertTrue(probe)
            with self.assertRaises(remote.CircuitOpen):
                other.admit("gpu", 60)
            self.gate.finish("gpu", epoch, False)
        with patch.object(remote.time, "time", return_value=110):
            with self.assertRaises(remote.CircuitOpen):
                other.admit("gpu", 60)
        with patch.object(remote.time, "time", return_value=131):
            epoch, probe = other.admit("gpu", 60)
            self.assertTrue(probe)
            other.finish("gpu", epoch, True)
            self.assertEqual(self.gate.admit("gpu", 60), (epoch, False))

    def test_healthy_calls_remain_concurrent_and_late_success_cannot_reset_failure(self):
        epoch, _ = self.gate.admit("gpu", 60)
        self.gate.finish("gpu", epoch, True)
        self.assertEqual(self.gate.admit("gpu", 60), (epoch, False))
        self.assertEqual(self.gate.admit("gpu", 60), (epoch, False))
        self.gate.finish("gpu", epoch, False)
        self.gate.finish("gpu", epoch, True)
        with self.assertRaises(remote.CircuitOpen):
            self.gate.admit("gpu", 60)

    def test_abandoned_probe_expires_and_repeated_failure_backs_off(self):
        with patch.object(remote.time, "time", return_value=100):
            old, _ = self.gate.admit("gpu", 10)
        with patch.object(remote.time, "time", return_value=121):
            epoch, probe = self.gate.admit("gpu", 10)
            self.assertTrue(probe)
            self.gate.finish("gpu", old, True)
            self.gate.finish("gpu", epoch, False)
        with patch.object(remote.time, "time", return_value=152):
            epoch, _ = self.gate.admit("gpu", 10)
            self.gate.finish("gpu", epoch, False)
        with patch.object(remote.time, "time", return_value=183):
            with self.assertRaises(remote.CircuitOpen):
                self.gate.admit("gpu", 10)

    def test_concurrent_initial_calls_admit_one_probe(self):
        def admit(_):
            try:
                return self.gate.admit("gpu", 60)[1]
            except remote.CircuitOpen:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(admit, range(16))), 1)

    def test_network_probe_failure_skips_work_and_is_shared(self):
        with patch.object(remote, "circuit", self.gate), patch.object(remote, "_probe", side_effect=TimeoutError()) as probe:
            with self.assertRaises(TimeoutError), remote.remote_attempt("asr", "http://gpu", "token", 60):
                self.fail("network work should not start")
            with self.assertRaises(remote.CircuitOpen), remote.remote_attempt("asr", "http://gpu", "token", 60):
                self.fail("cooldown should use CPU")
            self.assertEqual(probe.call_count, 1)

    def test_network_aliases_share_state_without_persisting_credentials(self):
        secret = "private-test-secret"
        left = remote._scope("asr", "http://172.18.0.1:18867", secret)
        self.assertEqual(left, remote._scope("asr", "http://172.19.0.1:18867/", secret))
        self.assertNotEqual(left, remote._scope("ocr", "http://172.19.0.1:18867", secret))
        self.assertNotEqual(left, remote._scope("asr", "http://172.19.0.1:18867", "rotated"))
        self.gate.admit(left, 60)
        self.assertNotIn(secret.encode(), (Path(self.temp.name)/"gpu-circuit.sqlite3").read_bytes())

    def test_bookkeeping_failure_preserves_success_and_original_remote_error(self):
        with (patch.object(remote, "circuit", self.gate), patch.object(remote, "_probe"),
              patch.object(self.gate, "finish", side_effect=sqlite3.OperationalError("locked"))):
            with remote.remote_attempt("asr", "http://gpu", "token", 60):
                pass
            with self.assertRaisesRegex(ValueError, "original"), remote.remote_attempt("ocr", "http://gpu", "token", 90):
                raise ValueError("original")
