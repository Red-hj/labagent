"""Keep the optional computer module separate from the diagnosis workflow."""
from html.parser import HTMLParser
from pathlib import Path
import unittest


class Structure(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.stack, self.parents = [], {}
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.parents[attrs["id"]] = list(self.stack)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, attrs.get("id")))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


class StudentMarkupTests(unittest.TestCase):
    def test_main_controls_stay_outside_optional_connection_panel(self):
        source = (Path(__file__).resolve().parents[1] / "web/student.html").read_text(encoding="utf-8")
        structure = Structure(source)
        for control in ("problem", "analyze", "message", "demo-panel", "open-knowledge"):
            with self.subTest(control=control):
                self.assertNotIn(("dialog", "lab-connection"), structure.parents[control])
        for control in ("lab-connection-view",):
            with self.subTest(control=control):
                self.assertIn(("dialog", "lab-connection"), structure.parents[control])
                self.assertNotIn(("div", "diagnosis-panel"), structure.parents[control])
