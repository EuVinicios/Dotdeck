"""Single instance without a D-Bus name (the Snap): lock, hand-off and stale sockets."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import run_until

from src.core.instance import SingleInstance


class SingleInstanceTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="dotdeck-instance-"))
        self.first = SingleInstance(self.folder)
        self.received = []

    def tearDown(self):
        self.first.close()

    def test_second_launch_hands_its_command_to_the_first(self):
        self.assertTrue(self.first.acquire())
        self.first.listen(self.received.append)
        second = SingleInstance(self.folder)
        self.assertFalse(second.acquire())
        self.assertTrue(second.send("toggle-mute"))
        self.assertTrue(second.send("activate"))
        self.assertTrue(run_until(lambda: len(self.received) == 2, 3))
        self.assertEqual(self.received, ["toggle-mute", "activate"])

    def test_unknown_commands_are_refused(self):
        with self.assertRaises(ValueError):
            SingleInstance(self.folder).send("rm -rf")

    def test_lock_is_released_on_close_and_a_stale_socket_is_replaced(self):
        self.assertTrue(self.first.acquire())
        self.first.listen(self.received.append)
        self.first.close()
        self.first.socket_path.touch()   # as if the process had crashed
        again = SingleInstance(self.folder)
        self.assertTrue(again.acquire())
        again.listen(self.received.append)
        self.assertTrue(SingleInstance(self.folder).send("toggle"))
        self.assertTrue(run_until(lambda: self.received == ["toggle"], 3))
        again.close()

    def test_nobody_listening(self):
        self.assertFalse(SingleInstance(self.folder).send("activate", timeout=1))


if __name__ == "__main__":
    unittest.main()
