"""Download the exact connector bundle and run it outside the application tree."""
import base64
from io import BytesIO
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
from zipfile import ZipFile

from labagent.server import create_server

ROOT = Path(__file__).resolve().parents[1]


class ConnectorPackageTests(unittest.TestCase):
    def test_authenticated_download_runs_as_a_standalone_stdlib_connector(self):
        folder = ROOT / "runtime/tests"
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temp:
            server = create_server(0, Path(temp) / "workspace.sqlite",
                                   diagnosis_backend="local", team_password="connector-test")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/lab/connector"
                opener = build_opener(ProxyHandler({}))
                with self.assertRaises(HTTPError) as denied:
                    opener.open(url)
                self.assertEqual(denied.exception.code, 401)
                auth = base64.b64encode(b"labagent:connector-test").decode()
                with opener.open(Request(url, headers={"Authorization": "Basic " + auth})) as response:
                    self.assertEqual(response.headers["Content-Type"], "application/zip")
                    self.assertIn("labagent-connector.zip", response.headers["Content-Disposition"])
                    archive = response.read()
                unpacked = Path(temp) / "unpacked"
                with ZipFile(BytesIO(archive)) as package:
                    self.assertEqual(set(package.namelist()), {
                        "connect.cmd", "README.txt", "labagent/__init__.py",
                        "labagent/device_console.py", "labagent/mcp_client.py",
                        "scripts/device_mcp.py", "scripts/connect_lab.py"})
                    package.extractall(unpacked)
                setup = subprocess.run([sys.executable, "-X", "utf8", "-B", "scripts/connect_lab.py", "--setup"],
                    input="R1\n127.0.0.1\n2000\n\n", cwd=unpacked, capture_output=True, text=True,
                    encoding="utf-8", timeout=8)
                self.assertEqual(setup.returncode, 0, setup.stderr)
                catalog = subprocess.run([sys.executable, "-X", "utf8", "-B", "scripts/connect_lab.py", "--check"],
                    cwd=unpacked, capture_output=True, text=True, encoding="utf-8", timeout=8)
                self.assertEqual(catalog.returncode, 0, catalog.stderr)
                self.assertIn("lab_console_r1", catalog.stdout)
                self.assertIn("display ip interface brief", catalog.stdout)
                self.assertIn("自检通过", catalog.stdout)
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
