"""Load the homepage's actual script/style URLs through the application server."""
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import urlopen

from labagent.server import create_server

ROOT = Path(__file__).resolve().parents[1]


class AssetReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src", "").startswith("/"):
            self.urls.append((attrs["src"], "javascript"))
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.urls.append((attrs["href"], "text/css"))


class WebAssetsTests(unittest.TestCase):
    def test_homepage_scripts_and_styles_are_served(self):
        folder = ROOT / "runtime/tests"
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temp:
            server = create_server(0, Path(temp) / "workspace.sqlite", diagnosis_backend="local")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urlopen(base + "/") as response:
                    parser = AssetReferences()
                    parser.feed(response.read().decode("utf-8"))
                self.assertIn(("/react-conversation.js", "javascript"), parser.urls)
                self.assertIn(("/execution-state.js", "javascript"), parser.urls)
                for url, mime in parser.urls:
                    with self.subTest(url=url), urlopen(base + url) as response:
                        self.assertEqual(response.status, 200)
                        self.assertIn(mime, response.headers["Content-Type"])
                        self.assertGreater(len(response.read()), 0)
            finally:
                server.shutdown()
                thread.join()
                server.server_close()
