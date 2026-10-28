import unittest
from unittest import mock
import risk_main


class KeepAwakeTests(unittest.TestCase):
    def test_windows_keeps_the_system_awake_while_the_desk_runs(self):
        with mock.patch.object(risk_main.os, "name", "nt"), mock.patch(
            "ctypes.windll", create=True
        ) as w:
            w.kernel32.SetThreadExecutionState.return_value = 0x80000000
            self.assertTrue(risk_main.keep_awake())
            w.kernel32.SetThreadExecutionState.assert_called_once_with(0x80000001)

    def test_other_systems_are_left_alone(self):
        with mock.patch.object(risk_main.os, "name", "posix"):
            self.assertFalse(risk_main.keep_awake())


if __name__ == "__main__":
    unittest.main()
