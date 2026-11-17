import json
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from labagent.demos import network_access_demo
from labagent.investigation import source_bundle, execute_tool
from labagent.network_map import build_map, explain_path
from labagent.rooms import Rooms, RoomConflict
from labagent.server import create_server
from labagent.store import Store

ROOT = Path(__file__).resolve().parents[1]


def network(materials):
    return build_map(source_bundle({"configs": materials})[0])


class MapTests(unittest.TestCase):
    def test_measured_lab02_map_matches_both_directions_and_raw_lines(self):
        root = ROOT / "data/team_cases/lab_02/map_materials"
        for phase in ("fault", "repaired"):
            materials = {file.name: file.read_text(encoding="utf-8") for file in sorted((root / phase).iterdir())}
            data = network(materials)
            self.assertEqual({n["id"] for n in data["nodes"]}, {"PC1", "R1", "R2", "PC2"})
            self.assertEqual({frozenset((e["source"], e["target"])) for e in data["links"]},
                             {frozenset(edge) for edge in (("PC1", "R1"), ("R1", "R2"), ("R2", "PC2"))})
            for source, near, far, destination, target in (("PC1", "R1", "R2", "PC2", "192.168.20.10"),
                                                           ("PC2", "R2", "R1", "PC1", "192.168.10.10")):
                with self.subTest(phase=phase, source=source):
                    result = explain_path(data, source, target)
                    if phase == "fault":
                        self.assertEqual((result["status"], result["path"]), ("failed", [source, near]))
                        self.assertTrue(any(s["device"] == near and s["title"] == "检查目标路由" and s["evidence"] for s in result["steps"]))
                    else:
                        self.assertEqual((result["status"], result["path"]), ("passed", [source, near, far, destination]))
                        self.assertTrue(any("OSPF" in s["detail"] for s in result["steps"]))
                    for step in result["steps"]:
                        for ref in step["evidence"]:
                            self.assertEqual(materials[ref["device"]].splitlines()[ref["line"] - 1], ref["quote"])

    def test_gateway_failure_is_located_and_each_quote_matches_input(self):
        materials = network_access_demo()["stages"][0]["materials"]
        data = network(materials)
        self.assertEqual({n["id"] for n in data["nodes"]}, {"PC1", "SW1", "R1", "SRV1"})
        result = explain_path(data, "PC1", "10.30.0.10")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(s["title"] == "下一跳地址解析失败" and "10.20.0.254" in s["detail"] for s in result["steps"]))
        for step in result["steps"]:
            for ref in step["evidence"]:
                self.assertEqual(materials[ref["device"]].splitlines()[ref["line"] - 1], ref["quote"])

    def test_repaired_material_changes_path_and_observed_result(self):
        data = network(network_access_demo()["stages"][1]["materials"])
        result = explain_path(data, "PC1", "10.30.0.10")
        self.assertEqual(result["status"], "passed")
        self.assertIn("R1", result["path"])
        self.assertIn("SW1", result["path"])
        self.assertEqual(result["path"][-1], "SRV1")
        self.assertFalse(any(s["status"] == "failed" for s in result["steps"]))

    def test_longest_prefix_and_route_loop(self):
        data = network({"R1.cfg": "sysname R1\ninterface GE0/0/0\n ip address 10.0.0.1 255.255.255.0\n#\n<R1>display ip routing-table\nDestination/Mask Proto Pre Cost Flags NextHop Interface\n0.0.0.0/0 Static 60 0 RD 10.0.0.9 GE0/0/0\n192.168.0.0/16 OSPF 10 2 D 10.0.0.2 GE0/0/0",
                        "R2.cfg": "sysname R2\ninterface GE0/0/0\n ip address 10.0.0.2 255.255.255.0\n#\n<R2>display ip routing-table\nDestination/Mask Proto Pre Cost Flags NextHop Interface\n192.168.0.0/16 OSPF 10 2 D 10.0.0.1 GE0/0/0"})
        result = explain_path(data, "R1", "192.168.2.10")
        self.assertIn("192.168.0.0/16", result["steps"][0]["detail"])
        self.assertEqual(result["path"], ["R1", "R2"])
        self.assertEqual(result["steps"][-1]["title"], "下一跳重复")

    def test_configuration_path_alone_does_not_pass_experiment(self):
        data = network({"R1.cfg": "sysname R1\ninterface GE0/0/0\n ip address 10.0.0.1 255.255.255.0", "PC1.txt": "IPv4 Address: 10.0.0.2\nSubnet Mask: 255.255.255.0"})
        result = explain_path(data, "R1", "10.0.0.2")
        self.assertEqual(result["status"], "needs_evidence")
        self.assertEqual(result["path"], ["R1", "PC1"])

    def test_missing_next_hop_is_evidence_request_not_fault(self):
        result = explain_path(network({"PC1.txt": "IPv4 Address: 10.0.0.2\nSubnet Mask: 255.255.255.0\nDefault Gateway: 10.0.0.1"}), "PC1", "192.168.1.1")
        self.assertEqual(result["status"], "needs_evidence")
        self.assertIn("10.0.0.1", result["steps"][-1]["detail"])

    def test_off_subnet_gateway_and_shutdown(self):
        result = explain_path(network({"PC1.txt": "IPv4 Address: 10.0.0.2\nSubnet Mask: 255.255.255.0\nDefault Gateway: 192.168.0.1"}), "PC1", "172.16.1.1")
        self.assertEqual(result["steps"][-1]["title"], "网关不在本地网段")
        result = explain_path(network({"R1.cfg": "sysname R1\ninterface GE0/0/0\n ip address 10.0.0.1 255.255.255.0\n shutdown"}), "R1", "10.0.0.2")
        self.assertEqual(result["status"], "failed")

    def test_invalid_address_and_unmapped_source(self):
        data = network({"R1.cfg": "sysname R1"})
        for source, target in (("R1", "999.1.1.1"), ("missing", "1.1.1.1")):
            with self.assertRaises(ValueError):
                explain_path(data, source, target)

    def test_successful_ping_does_not_reopen_missing_path_details(self):
        data = network({"PC1.txt": "IPv4 Address: 10.0.0.2\nSubnet Mask: 255.255.255.0\nPC1执行ping 192.168.2.2：发送4个，收到4个。"})
        result = explain_path(data, "PC1", "192.168.2.2")
        self.assertEqual(result["status"], "passed")
        self.assertFalse(any(s["status"] == "needs_evidence" or "补充" in s["detail"] for s in result["steps"]))

    def test_tool_exposes_path_and_marks_returned_evidence_as_read(self):
        payload = {"configs": network_access_demo()["stages"][0]["materials"]}
        sources = source_bundle(payload)[0]; seen = set()
        result, terminal = execute_tool("explain_network_path", {"source": "PC1", "target": "10.30.0.10"}, payload, sources, seen)
        self.assertFalse(terminal)
        self.assertIn(("config:1", 35), seen)
        self.assertEqual(result["status"], "failed")

    def test_screenshot_prompt_names_device_without_turning_issued_command_into_config(self):
        data = network({"error-截图识别.log": "[R2-GigabitEthernet0/0/0]ip address 10.0.0.1 255.255.255.0\nError: Wrong parameter"})
        device = next(n for n in data["nodes"] if n["id"] == "R2")
        self.assertEqual(device["interfaces"], [])
        self.assertEqual(device["evidence"][0]["line"], 1)


class RoomTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / "runtime/tests"; folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.store = Store(Path(self.temp.name) / "rooms.sqlite3")
        self.rooms = Rooms(self.store)
        self.room = self.rooms.create("网络实验", "黄泓钧")

    def tearDown(self):
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to((ROOT / "runtime/tests").resolve()))
        self.temp.cleanup()

    def edit(self, actor, name, text, revision=0):
        return self.rooms.update(self.room["id"], {"actor": actor, "member_id": actor, "action": "materials", "patches": [{"name": name, "content": text, "base_revision": revision}]})

    def test_three_members_merge_different_materials(self):
        self.edit("黄泓钧", "PC1.txt", "IP配置")
        self.edit("赵亦康", "R1.cfg", "接口配置")
        value = self.edit("付雨航", "ping.log", "测试结果")
        self.assertEqual(value["revision"], 3)
        self.assertEqual(len(value["members"]), 3)
        self.assertEqual(value["materials"]["R1.cfg"]["author"], "赵亦康")
        self.assertEqual(Rooms(self.store).get(self.room["id"]), value)

    def test_conflicting_file_does_not_overwrite_and_transaction_is_atomic(self):
        self.edit("甲", "R1.cfg", "旧配置")
        self.edit("乙", "R1.cfg", "队友配置", 1)
        with self.assertRaises(RoomConflict):
            self.rooms.update(self.room["id"], {"actor": "甲", "member_id": "甲", "action": "materials", "patches": [
                {"name": "extra.log", "content": "新输出", "base_revision": 0},
                {"name": "R1.cfg", "content": "冲突配置", "base_revision": 1}]})
        value = self.rooms.get(self.room["id"])
        self.assertNotIn("extra.log", value["materials"])
        self.assertEqual(value["materials"]["R1.cfg"]["content"], "队友配置")

    def test_delete_and_recreate_cannot_reuse_old_revision(self):
        self.edit("甲", "R1.cfg", "第一版")
        self.edit("乙", "R1.cfg", None, 1)
        self.edit("乙", "R1.cfg", "重新采集", 2)
        with self.assertRaises(RoomConflict):
            self.edit("甲", "R1.cfg", "过期修改", 1)

    def test_parallel_independent_updates_are_preserved(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            values = list(pool.map(lambda i: self.edit("队友" + str(i), str(i) + ".log", "输出"), range(6)))
        latest = self.rooms.get(self.room["id"])
        self.assertEqual(len(values), 6)
        self.assertEqual(len(latest["materials"]), 6)
        self.assertEqual(latest["revision"], 6)

    def test_task_assignment_and_completion(self):
        value = self.rooms.update(self.room["id"], {"actor": "黄泓钧", "member_id": "a", "action": "task", "title": "采集R2邻居", "assignee": "赵亦康"})
        task = value["tasks"][0]
        value = self.rooms.update(self.room["id"], {"actor": "赵亦康", "member_id": "b", "action": "complete", "task_id": task["id"]})
        self.assertTrue(value["tasks"][0]["done"])
        self.assertEqual(value["activity"][-1]["actor"], "赵亦康")

    def test_diagnosis_reads_shared_snapshot_and_rejects_stale_revision(self):
        value = self.edit("甲", "R1.cfg", "共享配置")
        payload = {"room_id": value["id"], "room_revision": value["revision"], "room_actor": "甲", "configs": {"伪造": "过期"}, "question": "怎么改"}
        result = self.rooms.prepare_diagnosis(payload)
        self.assertEqual(result["configs"], {"R1.cfg": "共享配置"})
        self.edit("乙", "R1.cfg", "新输出", 1)
        with self.assertRaises(RoomConflict): self.rooms.prepare_diagnosis(payload)

    def test_old_diagnosis_cannot_replace_current_shared_result(self):
        first = self.edit("甲", "R1.cfg", "配置")
        payload = {"room_id": first["id"], "room_revision": 1, "room_actor": "甲"}
        self.rooms.record_diagnosis(payload, {"session_id": "a" * 32})
        self.edit("乙", "R1.cfg", "新配置", 1)
        self.rooms.record_diagnosis(payload, {"session_id": "b" * 32})
        self.assertEqual(self.rooms.get(first["id"])["session"]["session_id"], "a" * 32)

    def test_unchanged_question_does_not_block_independent_material_update(self):
        value = self.rooms.update(self.room["id"], {"actor": "甲", "member_id": "a", "action": "materials", "question": "访问服务器", "base_question": ""})
        value = self.edit("乙", "R2.cfg", "设备配置")
        self.assertEqual(value["question"], "访问服务器")

    def test_same_room_continues_latest_shared_session(self):
        value = self.edit("甲", "R1.cfg", "配置")
        self.rooms.record_diagnosis({"room_id": value["id"], "room_revision": 1, "room_actor": "甲"}, {"session_id": "a" * 32})
        prepared = self.rooms.prepare_diagnosis({"room_id": value["id"], "room_revision": 1, "room_actor": "乙", "session_id": "b" * 32})
        self.assertEqual(prepared["session_id"], "a" * 32)


class WorkspaceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = ROOT / "runtime/tests"; folder.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(dir=folder)
        cls.server = create_server(0, Path(cls.temp.name) / "workspace.sqlite3")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(5)
        assert Path(cls.temp.name).resolve().is_relative_to((ROOT / "runtime/tests").resolve())
        cls.temp.cleanup()

    def call(self, path, payload=None):
        req = Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=15) as response: return json.loads(response.read())

    def test_network_map_and_challenge_are_available_without_model_calls(self):
        result = self.call("/api/network/inspect", {"materials": network_access_demo()["stages"][0]["materials"], "source": "PC1", "target": "10.30.0.10"})
        self.assertEqual(result["explanation"]["status"], "failed")
        demo = self.call("/api/challenges/new")
        self.assertNotIn("answer", demo)
        self.assertFalse(self.call("/api/challenges/check", {"id": demo["id"], "choice": 1})["correct"])
        self.assertTrue(self.call("/api/challenges/check", {"id": demo["id"], "choice": 0})["correct"])

    def test_room_diagnosis_links_history_and_conflict_is_409(self):
        room = self.call("/api/rooms", {"name": "三人联调", "actor": "甲"})
        path = "/api/rooms/" + room["id"]
        materials = network_access_demo()["stages"][0]["materials"]
        update = {"actor": "甲", "member_id": "a", "action": "materials", "patches": [{"name": k, "content": v, "base_revision": 0} for k, v in materials.items()]}
        self.call(path, update)
        result = self.call("/api/diagnose", {"diagnostic_mode": "open_network", "use_llm": False, "room_id": room["id"], "room_revision": 1, "room_actor": "甲", "question": "PC1为什么无法访问服务器？"})
        room = self.call(path)
        self.assertEqual(room["session"]["session_id"], result["session_id"])
        with self.assertRaises(HTTPError) as denied:
            self.call("/api/sessions/" + result["session_id"])
        self.assertEqual(denied.exception.code, 404)
        history = self.call("/api/sessions/" + result["session_id"] + "?room=" + room["id"])
        self.assertEqual(history["attempts"][0]["input"]["room_actor"], "甲")
        with self.assertRaises(HTTPError) as caught: self.call(path, update)
        self.assertEqual(caught.exception.code, 409)


if __name__ == "__main__": unittest.main()
