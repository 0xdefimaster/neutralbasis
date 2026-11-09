import faulthandler, os, subprocess, sys, tempfile, textwrap, unittest
from unittest import mock
import risk_main


class CrashLogTests(unittest.TestCase):
    def tearDown(self):
        # Give faulthandler back to the test runner's stderr and close our file.
        import runtime_metrics

        runtime_metrics.STALL_LOG = None
        f, risk_main.CRASH_LOG = risk_main.CRASH_LOG, None
        faulthandler.enable(file=sys.__stderr__, all_threads=True)
        if f is not None:
            f.close()

    def test_each_run_gets_its_own_file_and_earlier_files_are_kept(self):
        with tempfile.TemporaryDirectory() as d:
            first = risk_main.enable_crash_log(d, at=1_800_000_000, pid=11)
            risk_main.CRASH_LOG.close()
            with open(first, "a", encoding="utf8") as f:
                f.write("earlier run evidence\n")
            second = risk_main.enable_crash_log(d, at=1_800_000_060, pid=12)
            self.assertNotEqual(first, second)
            self.assertTrue(faulthandler.is_enabled())
            with open(first, encoding="utf8") as f:
                self.assertIn("earlier run evidence", f.read())
            # Same second and pid again: appended, never truncated.
            risk_main.CRASH_LOG.close()
            again = risk_main.enable_crash_log(d, at=1_800_000_060, pid=12)
            self.assertEqual(again, second)
            with open(second, encoding="utf8") as f:
                self.assertEqual(f.read().count("run started"), 2)
            risk_main.CRASH_LOG.close()
            risk_main.CRASH_LOG = None
            faulthandler.enable(file=sys.__stderr__, all_threads=True)

    def test_dump_reaches_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = risk_main.enable_crash_log(d, pid=13)
            faulthandler.dump_traceback(file=risk_main.CRASH_LOG, all_threads=True)
            risk_main.CRASH_LOG.flush()
            with open(path, encoding="utf8") as f:
                text = f.read()
            self.assertIn("test_dump_reaches_the_file", text)
            risk_main.CRASH_LOG.close()
            risk_main.CRASH_LOG = None
            faulthandler.enable(file=sys.__stderr__, all_threads=True)


@unittest.skipUnless(
    os.environ.get("NB_CRASH_TEST") == "1",
    "Gerçek native çökme testi (alt süreç; Windows hata raporu üretebilir): NB_CRASH_TEST=1",
)
class IsolatedCrashTests(unittest.TestCase):
    def test_native_crash_in_a_worker_thread_leaves_python_stacks_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            code = textwrap.dedent(
                f"""
                import ctypes, faulthandler, os, threading, time
                if os.name == "nt":
                    # No Windows error dialog for this deliberate crash.
                    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)
                import risk_main
                risk_main.enable_crash_log({d!r})
                def rfq_like_worker():
                    # A real access violation (0xC0000005), the 2026-11-09 crash type;
                    # _sigsegv() takes the signal path, which is not what happened.
                    faulthandler._read_null()
                t = threading.Thread(target=rfq_like_worker, name="crash-worker")
                t.start(); t.join()
                """
            )
            r = subprocess.run(
                [sys.executable, "-c", code],
                cwd=os.path.dirname(os.path.abspath(risk_main.__file__)),
                capture_output=True,
                timeout=60,
            )
            self.assertNotEqual(r.returncode, 0)
            logs = [n for n in os.listdir(d) if n.startswith("faulthandler-")]
            self.assertEqual(len(logs), 1)
            with open(os.path.join(d, logs[0]), encoding="utf8") as f:
                text = f.read()
            self.assertTrue(
                "Windows fatal exception: access violation" in text
                or "Fatal Python error" in text,
                text,
            )
            self.assertIn("rfq_like_worker", text)
            self.assertIn("Current thread", text)


class KeepAwakeTests(unittest.TestCase):
    def test_windows_keeps_the_system_awake_while_the_desk_runs(self):
        with mock.patch.object(risk_main.os, "name", "nt"), mock.patch(
            "ctypes.windll", create=True
        ) as w:
            # It returns the previous state, which may be zero.
            w.kernel32.SetThreadExecutionState.return_value = 0
            self.assertTrue(risk_main.keep_awake())
            w.kernel32.SetThreadExecutionState.assert_called_once_with(0x80000001)

    def test_other_systems_are_left_alone(self):
        with mock.patch.object(risk_main.os, "name", "posix"):
            self.assertFalse(risk_main.keep_awake())


if __name__ == "__main__":
    unittest.main()
