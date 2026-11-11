"""Three-router demonstration inputs. Outputs are synthetic, clearly labeled."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from copy import deepcopy
import json
from labagent.engine import fingerprint


def router(name, rid, interfaces):
    lines = ["#", "sysname " + name, "#"]
    for interface, ip, area in interfaces:
        lines += ["interface " + interface, f" ip address {ip} 255.255.255.0", " undo shutdown", f" ospf enable 1 area {area}", "#"]
    lines += ["interface LoopBack0", f" ip address {rid} 255.255.255.255", " ospf enable 1 area 0", "#", f"ospf 1 router-id {rid}", "#", "return"]
    return "\n".join(lines) + "\n"


def neighbor(local, remote, rid):
    return f"OSPF Process 1 with Router ID {rid}\n Area 0.0.0.0 interface {local}(GigabitEthernet0/0/0)'s neighbors\n Router ID: 9.9.9.9 Address: {remote}\n State: Full Mode:Nbr is Master\n"


def main():
    topology = {"links": [
        {"id": "r1-r2", "endpoints": [{"device": "R1", "interface": "GE0/0/0"}, {"device": "R2", "interface": "GE0/0/0"}]},
        {"id": "r2-r3", "endpoints": [{"device": "R2", "interface": "GE0/0/1"}, {"device": "R3", "interface": "GE0/0/0"}]},
    ], "ping_targets": [{"source": "R1", "target": "3.3.3.3"}]}
    base = {"question": "为什么 R1 使用 LoopBack0 地址 ping 不通 R3 的 3.3.3.3？", "configs": {
        "R1": router("R1", "1.1.1.1", [("GE0/0/0", "10.12.0.1", 0)]),
        "R2": router("R2", "2.2.2.2", [("GE0/0/0", "10.12.0.2", 0), ("GE0/0/1", "10.23.0.2", 0)]),
        "R3": router("R3", "3.3.3.3", [("GE0/0/0", "10.23.0.3", 0)])},
        "topology": topology, "requirements": {"expected_area": "0"}, "routing_queries": [{"device": "R1", "target": "3.3.3.3"}, {"device": "R3", "target": "1.1.1.1"}],
        "observations": {"neighbors": [], "pings": []}, "hint_level": 2, "evidence_kind": "demo"}
    first = deepcopy(base)
    first["configs"]["R2"] = first["configs"]["R2"].replace("ip address 10.23.0.2 255.255.255.0\n undo shutdown\n ospf enable 1 area 0", "ip address 10.23.0.2 255.255.255.0\n undo shutdown\n ospf enable 1 area 1")
    first.update(title="阶段1：定位 R2—R3 区域错误", description="预置三路由器演示。当前只有配置，系统应定位区域错误并等待修改。")
    stages = [first]
    header = "Routing Tables: Public\nDestination/Mask Proto Pre Cost Flags NextHop Interface\n"
    for success in (False, True):
        stage = deepcopy(base)
        stage.update(title="阶段3：新观测核对通过" if success else "阶段2：邻居正常，目标路由仍缺失", description="所有命令输出均为合成演示数据，不是实际设备采集。按下一阶段可继续同一会话。")
        digest = fingerprint(stage)
        rows = [
            {"device": "R1", "command": "display ospf peer", "text": neighbor("10.12.0.1", "10.12.0.2", "1.1.1.1")},
            {"device": "R2", "command": "display ospf peer", "text": neighbor("10.23.0.2", "10.23.0.3", "2.2.2.2")},
            {"device": "R1", "command": "display ip routing-table", "complete": True, "text": header + "10.12.0.0/24 Direct 0 0 D 10.12.0.1 GE0/0/0\n" + ("3.3.3.3/32 OSPF 10 2 D 10.12.0.2 GE0/0/0\n" if success else "")},
            {"device": "R3", "command": "display ip routing-table", "complete": True, "text": header + "1.1.1.1/32 OSPF 10 2 D 10.23.0.2 GE0/0/0\n"},
            {"device": "R1", "command": "ping -a 1.1.1.1 3.3.3.3", "text": "PING 3.3.3.3: 56 data bytes, press CTRL_C to break\n5 packet(s) transmitted\n" + ("5" if success else "0") + " packet(s) received\n"},
        ]
        stage["command_outputs"] = [{**row, "config_fingerprint": digest, "kind": "demo"} for row in rows]
        stages.append(stage)
    bundle = {"title": "R1—R2—R3：完整诊断与复测（演示）", "description": "三个阶段的合成演示：区域故障 → 邻居正常但缺路由 → 新观测通过。不能用作实测准确率或现场设备运行证明。", "stages": stages}
    folder = ROOT / "data" / "scenarios"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "three_routers.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Created 3-stage synthetic workflow demonstration")


if __name__ == "__main__":
    main()
