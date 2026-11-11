"""Rebuild hand-defined regression fixtures, not an independent benchmark."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import copy
import json
from labagent.engine import fingerprint


def config(ip, router, area="0", process=1, interface="GigabitEthernet0/0/0", network="10.12.0.0"):
    lan = "10" if router == "R1" else "20" if router == "R2" else None
    lan_interface = f"interface GigabitEthernet0/0/2\n ip address 192.168.{lan}.1 255.255.255.0\n undo shutdown\n#\n" if lan else ""
    lan_network = f"  network 192.168.{lan}.0 0.0.0.255\n" if lan else ""
    return f"#\nsysname {router}\n#\ninterface {interface}\n ip address {ip} 255.255.255.0\n undo shutdown\n#\n{lan_interface}ospf {process} router-id {'1.1.1.1' if router == 'R1' else '2.2.2.2'}\n area {area}\n  network {network} 0.0.0.255\n{lan_network}#\nreturn\n"


def base():
    return {"question": "PC1 无法访问 PC2，请检查这个 OSPF 单区域实验。",
            "configs": {"R1": config("10.12.0.1", "R1"), "R2": config("10.12.0.2", "R2")},
            "topology": {"links": [{"id": "r1-r2", "endpoints": [{"device": "R1", "interface": "GE0/0/0"}, {"device": "R2", "interface": "GE0/0/0"}], "ospf": True}], "ping_targets": [{"source": "PC1", "target": "192.168.20.10"}]},
            "requirements": {"expected_area": "0", "title": "OSPF 单区域互联"}, "hint_level": 1,
            "observations": {"neighbors": [], "pings": []}, "evidence_kind": "demo"}


def main():
    folder = ROOT / "data" / "cases"
    folder.mkdir(parents=True, exist_ok=True)
    cases = []
    def add(name, title, description, payload, codes=(), status="fault_found", unknowns=()):
        payload.update(title=title, description=description, expected={"codes": sorted(codes), "unknown_codes": sorted(unknowns), "status": status})
        (folder / (name + ".json")).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        cases.append(name)
    p = base(); p["configs"]["R2"] = config("10.12.0.2", "R2", "1")
    add("01_area_mismatch", "01 · 互联区域不匹配", "本实验要求 Area 0，R2 被配置为 Area 1。", p, ("AREA_MISMATCH", "AREA_POLICY_VIOLATION"))
    add("02_static_ok", "02 · 正常配置，等待复测", "受支持范围内静态检查通过；没有运行观测，不应宣布修复。", base(), status="awaiting_verification")
    p = base(); p["observations"] = {"neighbors": [{"link_id": "r1-r2", "state": "Full"}], "pings": [{"source": "PC1", "target": "192.168.20.10", "sent": 5, "received": 5}], "config_fingerprint": fingerprint(p)}
    add("03_verified_demo", "03 · 用户提交观测通过（演示）", "这是预置演示观测，不是真实设备自动测试结果。", p, status="verified_from_observations")
    p = base(); p["configs"]["R2"] = config("10.13.0.2", "R2", network="10.13.0.0")
    add("04_subnet_mismatch", "04 · 互联接口网段错误", "两端接口不在同一子网。", p, ("SUBNET_MISMATCH",))
    p = base(); p["configs"]["R2"] = p["configs"]["R2"].replace("undo shutdown", "shutdown")
    add("05_shutdown", "05 · 接口被关闭", "R2 互连接口处于管理关闭状态。", p, ("INTERFACE_SHUTDOWN",))
    p = base(); p["configs"]["R2"] = p["configs"]["R2"].replace("network 10.12.0.0", "network 10.99.0.0")
    add("06_not_enabled", "06 · network 未匹配接口", "R2 的 OSPF network 未匹配互连接口主 IP。", p, ("OSPF_NOT_ENABLED",))
    p = base(); p["configs"]["R2"] = p["configs"]["R2"].replace("2.2.2.2", "1.1.1.1")
    add("07_router_id", "07 · Router ID 重复", "邻接两端显式配置了相同 Router ID。", p, ("DUPLICATE_ROUTER_ID",))
    p = base(); p["configs"]["R2"] = p["configs"]["R2"].replace(" undo shutdown", " undo shutdown\n ospf timer hello 5")
    add("08_timer", "08 · Hello 定时器不匹配", "互连接口两端 Hello 定时器不同。", p, ("TIMER_MISMATCH",))
    p = base(); p["configs"]["R2"] = p["configs"]["R2"].replace(" area 0", " silent-interface GE0/0/0\n area 0")
    add("09_silent", "09 · 互连接口被静默", "互连接口配置了 silent-interface。", p, ("SILENT_INTERFACE",))
    p = base(); del p["configs"]["R2"]
    add("10_missing", "10 · 缺少设备配置", "缺少 R2 配置，应要求补充，不能编造故障结论。", p, status="needs_information", unknowns=("MISSING_DEVICE",))
    p = base(); p["configs"]["R2"] = p["configs"]["R2"].replace(" undo shutdown", " undo shutdown\n ospf authentication-mode md5 1 cipher DEMO_ONLY")
    add("11_unsupported", "11 · 超出检查范围的认证配置", "当前版本不判断认证是否匹配，应标记待人工确认。", p, status="needs_information", unknowns=("UNSUPPORTED_CONFIG",))
    p = base(); p["configs"] = {k: v.replace(" area 0", " area 1") for k, v in p["configs"].items()}
    add("12_policy", "12 · 区域一致但违反实验要求", "两端均为 Area 1，邻居区域匹配，但实验要求 Area 0。", p, ("AREA_POLICY_VIOLATION",))
    p = base(); p["configs"]["R2"] = p["configs"]["R2"].replace("ospf 1 ", "ospf 9 ")
    add("13_process_ids", "13 · 不同进程号的正常配置", "OSPF 进程号是本地标识，两端不同不应误报。", p, status="awaiting_verification")
    p = base(); p["configs"]["R2"] = config("10.13.0.2", "R2", "1", network="10.13.0.0").replace("undo shutdown", "shutdown")
    add("14_multiple", "14 · 多个配置问题", "区域、接口网段和管理状态同时有问题。", p, ("AREA_MISMATCH", "AREA_POLICY_VIOLATION", "SUBNET_MISMATCH", "INTERFACE_SHUTDOWN"))
    p = base(); p["requirements"] = {"title": "合法多区域实验"}
    p["configs"]["R2"] = p["configs"]["R2"].replace("return", "interface GE0/0/1\n ip address 10.23.0.2 255.255.255.0\n ospf enable 1 area 1\n#\nreturn")
    p["configs"]["R3"] = config("10.23.0.3", "R3", "1", interface="GE0/0/1", network="10.23.0.0").replace("2.2.2.2", "3.3.3.3")
    p["topology"]["links"].append({"id": "r2-r3", "endpoints": [{"device": "R2", "interface": "GE0/0/1"}, {"device": "R3", "interface": "GE0/0/1"}], "expected_area": "1"})
    add("15_multiarea", "15 · 合法的多区域拓扑", "按互联链路比较区域，不能要求全网所有设备的区域相同。", p, status="awaiting_verification")
    p = base(); p["configs"]["R2"] = config("10.12.0.1", "R2")
    add("16_duplicate_ip", "16 · 互连接口 IP 重复", "同一链路两端使用了相同 IP 地址。", p, ("DUPLICATE_IP",))
    lab = ROOT / "data" / "lab"
    lab.mkdir(parents=True, exist_ok=True)
    for name, text in base()["configs"].items():
        (lab / (name + "_correct.cfg")).write_text(text, encoding="utf-8")
    (lab / "R2_area_fault.cfg").write_text(config("10.12.0.2", "R2", "1"), encoding="utf-8")
    print(f"Created {len(cases)} hand-defined regression cases in {folder}")


if __name__ == "__main__":
    main()
