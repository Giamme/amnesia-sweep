from __future__ import annotations

import unittest

try:
    import curses
except ImportError:  # e.g. a Python built without curses
    curses = None


@unittest.skipIf(curses is None, "curses is not available")
class KeyNameTests(unittest.TestCase):
    def test_curses_codes_map_to_the_key_names_the_browser_understands(self):
        from amnesia_sweep.tui import key_name

        cases = ((curses.KEY_UP, "up"), (curses.KEY_RIGHT, "right"), (curses.KEY_NPAGE, "pgdn"),
                 (10, "enter"), (13, "enter"), (127, "backspace"), (27, "esc"), (32, "space"),
                 (ord("d"), "d"), (ord("?"), "?"), (ord("U"), "U"), (0, None), (200, None))
        for code, name in cases:
            self.assertEqual(key_name(code), name, code)


if __name__ == "__main__":
    unittest.main()
