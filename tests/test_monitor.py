from __future__ import annotations

import unittest

from tests.helpers import run_until

from src.core import monitor  # noqa: E402
from src.core.store import Profile  # noqa: E402
from tests.fake_server import FakeOpenDots  # noqa: E402


def task(id_, status, interval=None, next_run=None, error=""):
    return monitor.TaskSnapshot(id_, f"prompt {id_}", status, interval, next_run, error)


class TaskEventsTest(unittest.TestCase):
    def events(self, before, after):
        return [(kind, t.id) for kind, t in
                monitor.task_events({t.id: t for t in before}, after)]

    def test_one_off_task_lifecycle(self):
        self.assertEqual(self.events([], [task("a", "queued")]), [])
        self.assertEqual(self.events([task("a", "queued")], [task("a", "running")]), [])
        self.assertEqual(self.events([task("a", "running")], [task("a", "completed")]),
                         [("completed", "a")])
        self.assertEqual(self.events([task("a", "queued")], [task("a", "completed")]),
                         [("completed", "a")])
        self.assertEqual(self.events([task("a", "running")], [task("a", "failed", error="x")]),
                         [("failed", "a")])
        self.assertEqual(self.events([task("a", "completed")], [task("a", "completed")]), [])

    def test_scheduled_task(self):
        self.assertEqual(self.events([task("s", "queued", 3600, 100)],
                                     [task("s", "running", 3600, 100)]), [("triggered", "s")])
        self.assertEqual(self.events([task("s", "running", 3600, 100)],
                                     [task("s", "queued", 3600, 3700)]), [("completed", "s")])
        # The whole run happened between two polls.
        self.assertEqual(self.events([task("s", "queued", 3600, 100)],
                                     [task("s", "queued", 3600, 3700)]), [("completed", "s")])
        self.assertEqual(self.events([task("s", "paused", 3600, 100)],
                                     [task("s", "paused", 3600, 100)]), [])

    def test_snapshot_parsing(self):
        parsed = monitor.TaskSnapshot.from_json({"id": "x", "prompt": "Summarize", "status":
                                                 "running", "intervalSeconds": 60,
                                                 "nextRunAt": 5, "error": None})
        self.assertEqual((parsed.interval, parsed.next_run_at, parsed.error), (60, 5, ""))
        self.assertIsNone(monitor.TaskSnapshot.from_json({"prompt": "no id"}))


class ChiefTest(unittest.TestCase):
    def test_choice(self):
        profile = Profile("p", "Home", "http://h")
        self.assertEqual(monitor.chief_dot(profile, ["Dot", "Chefe", "Zelador"]), "Chefe")
        self.assertEqual(monitor.chief_dot(profile, ["Writer", "Analyst"]), "Writer")
        self.assertIsNone(monitor.chief_dot(profile, []))
        profile.chief = "Zelador"
        self.assertEqual(monitor.chief_dot(profile, ["Dot", "Chefe", "Zelador"]), "Zelador")


class ServerMonitorTest(unittest.TestCase):
    def test_polls_and_reports_events(self):
        with FakeOpenDots(token="t" * 24, dots=["Chefe"]) as fake:
            fake.tasks = [{"id": "a", "prompt": "Weekly report", "status": "running"}]
            profile = Profile("p", "Home", fake.url)
            mon = monitor.ServerMonitor(profile, lambda cb: cb("t" * 24))
            events = []
            mon.connect("task-event", lambda _m, *args: events.append(args))
            mon.start()
            self.assertTrue(run_until(lambda: mon.status == "online", 10))
            self.assertEqual((mon.name, mon.dots, mon.active_tasks), ("OpenDots Test", ["Chefe"], 1))
            fake.tasks = [{"id": "a", "prompt": "Weekly report", "status": "completed"}]
            mon.poll_now()
            self.assertTrue(run_until(lambda: events, 10))
            self.assertEqual(events[0][:3], ("completed", "a", "Weekly report"))
            self.assertEqual(mon.active_tasks, 0)
            mon.stop()

    def test_token_and_offline_states(self):
        with FakeOpenDots(token="t" * 24) as fake:
            mon = monitor.ServerMonitor(Profile("p", "Home", fake.url), lambda cb: cb(None))
            mon.start()
            self.assertTrue(run_until(lambda: mon.status == "token", 10))
            mon.stop()
        mon = monitor.ServerMonitor(Profile("p", "Home", fake.url), lambda cb: cb(None))
        mon.start()
        self.assertTrue(run_until(lambda: mon.status == "offline", 15))
        mon.stop()


if __name__ == "__main__":
    unittest.main()
