"""The call panel and its level waves (src/ui/call_panel.py, ADR-029)."""

from __future__ import annotations

import unittest

from tests.helpers import run_until

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from src.ui.call_panel import CallPanel, LevelWave  # noqa: E402


class LevelWaveTest(unittest.TestCase):
    def test_the_newest_level_comes_in_on_the_right(self):
        wave = LevelWave(bars=4)
        wave.set_level(0.5)
        wave.step()
        wave.set_level(2.0)            # clamped to 1
        wave.step()
        self.assertEqual(wave.levels, [0.0, 0.0, 0.5, 1.0])
        wave.reset()
        self.assertEqual(wave.levels, [0.0] * 4)
        self.assertEqual(wave.measure(Gtk.Orientation.HORIZONTAL, -1)[0], 4 * 3 + 3 * 4)


class CallPanelTest(unittest.TestCase):
    def setUp(self):
        Adw.init()
        self.settings_opened = []
        self.panel = CallPanel(on_settings=lambda: self.settings_opened.append(True))
        self.entry = Gtk.Text()
        box = Gtk.Box()
        box.append(self.entry)
        self.window = Gtk.Window(child=box, default_width=500, default_height=400)
        self.panel.set_parent(box)
        self.window.present()

    def tearDown(self):
        self.panel.unparent()
        self.window.destroy()

    def test_the_transcript_keeps_what_was_said(self):
        self.panel.update(dot_name="Manutenção", pet_id="bolt", phase="listening",
                          status="Listening · 00:12", connecting=False, ending=False)
        self.assertEqual(self.panel.transcript.get_label(), "Nothing said yet")
        self.panel.add_caption("you", "How are the", False)
        self.panel.add_caption("you", "How are the backups?", True)
        self.panel.add_caption("dot", "All good", False)
        self.assertEqual(self.panel.lines(), ["You: How are the backups?",
                                              "Manutenção: All good"])
        self.panel.add_caption("dot", "All good, 14 copies.", True)
        self.assertEqual(self.panel.lines()[-1], "Manutenção: All good, 14 copies.")
        self.panel.reset()
        self.assertEqual(self.panel.lines(), [])

    def test_connecting_ending_and_muted(self):
        self.panel.update(dot_name="Pesquisa", pet_id="sky", phase="connecting",
                          status="Connecting…", connecting=True, ending=False)
        self.assertTrue(self.panel.progress.get_visible())
        self.assertEqual(self.panel.avatar.state, "call")
        self.panel.update(dot_name="Pesquisa", pet_id="sky", phase="speaking",
                          status="Speaking · 00:03", connecting=False, ending=False, muted=True)
        self.assertFalse(self.panel.progress.get_visible())
        self.assertEqual(self.panel.avatar.state, "speaking")
        self.assertEqual(self.panel.mute.get_icon_name(), "microphone-disabled-symbolic")
        self.panel.update(dot_name="Pesquisa", pet_id="sky", phase="ended",
                          status="Hanging up…", connecting=False, ending=True)
        self.assertFalse(self.panel.hang_up.get_sensitive())

    def test_it_floats_without_taking_the_keyboard(self):
        self.entry.grab_focus()
        self.assertTrue(run_until(lambda: self.window.get_focus() is self.entry, 2))
        self.panel.popup()
        self.assertTrue(run_until(self.panel.get_mapped, 3))
        run_until(lambda: False, 0.3)
        # The window keeps its focus widget (typing goes on in the page). Without a window
        # manager (Xvfb) the X server may still hand its focus to the panel's surface, so
        # the window's own focus widget is what is checked, not has_focus().
        self.assertIs(self.window.get_focus(), self.entry)
        self.panel.popdown()


if __name__ == "__main__":
    unittest.main()
