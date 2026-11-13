"""Production-readiness audit (after Phase 15): regressions for real faults found while
auditing. MOCK inputs only; no network, no clock or service change."""
import os
import subprocess
import unittest
from unittest import mock

from v2.control import doctor


class ClockStatusTests(unittest.TestCase):
    def run_with(self, stdout, stderr=b"", code=0):
        cp = subprocess.CompletedProcess(["w32tm"], code, stdout=stdout, stderr=stderr)
        with mock.patch.object(doctor.subprocess, "run", return_value=cp), \
                mock.patch.object(doctor.os, "name", "nt"):
            return doctor.clock_status()

    def test_stopped_time_service_in_a_local_code_page_never_crashes(self):
        """2026-11-14: w32tm printed Turkish OEM text (byte 0x8d) with the Windows Time
        service stopped; decoding raised in a reader thread, stdout became None and the
        whole Doctor crashed."""
        r = self.run_with(b"The following error occurred: Hizmet ba\x8dlat\x8dlmad\x8d. "
                          b"(0x80070426)\r\n", code=2147943462)
        self.assertEqual(r["health_status"], "UNVERIFIED")
        self.assertEqual(r["clock_confidence"], "NONE")
        self.assertIn("0x80070426", r["health_reason"])

    def test_synced_state_is_still_only_unverified_offline(self):
        r = self.run_with(b"Leap Indicator: 0\r\nLast Successful Sync Time: 14.11.2026 "
                          b"09:00:00\r\n")
        self.assertEqual(r["health_status"], "UNVERIFIED")       # offset needs NTP
        self.assertEqual(r["clock_confidence"], "UNKNOWN_OFFSET")

    def test_decoder_never_raises(self):
        for b in (b"\x8d\x9f\xff\xfe", b"", None, "ok".encode("utf-16")):
            self.assertIsInstance(doctor.decode_console(b), str)


if __name__ == "__main__":
    unittest.main()
