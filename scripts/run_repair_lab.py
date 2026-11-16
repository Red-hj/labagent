"""Run independent repair candidates in actual FRR Linux network namespaces."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from labagent.repair_lab import RUN_KIND, compare_run

IMAGE = "quay.io/frrouting/frr:10.6.2"


class FrrLab:
    def __init__(self, case, folder):
        self.case, self.folder = case, folder
        self.prefix = "labagent-" + uuid.uuid4().hex[:10]
        self.containers, self.links = {}, []

    def run(self, args, *, check=True):
        result = subprocess.run(args, capture_output=True, text=True, timeout=90)
        with (self.folder / "commands.log").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"argv": args, "code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}, ensure_ascii=False) + "\n")
        if check and result.returncode:
            raise RuntimeError(f"命令执行失败：{args[0]}：{result.stderr or result.stdout}")
        return result.stdout if check else result.stdout + result.stderr

    def cli(self, device, commands):
        args = ["docker", "exec", self.containers[device], "vtysh"]
        for command in commands:
            args.extend(["-c", command])
        output = self.run(args)
        if any(line.startswith(("% Unknown", "% Ambiguous", "% Command incomplete")) for line in output.splitlines()):
            raise RuntimeError("路由器返回命令错误：" + output)
        return output

    def setup(self):
        for device, config in self.case["routers"].items():
            name = self.prefix + "-" + device
            self.containers[device] = name
            self.run(["docker", "run", "-d", "--name", name, "--network", "none", "--privileged",
                      "--sysctl", "net.ipv4.ip_forward=1", "--label", "labagent.repair=" + self.prefix,
                      IMAGE, "/bin/sh", "-c", "sed -i 's/^ospfd=no/ospfd=yes/;s/^staticd=no/staticd=yes/' /etc/frr/daemons; exec /usr/lib/frr/docker-start"])
        pids = {device: self.run(["docker", "inspect", "--format", "{{.State.Pid}}", name]).strip() for device, name in self.containers.items()}
        for index, link in enumerate(self.case["links"]):
            ends = ["la" + self.prefix[-6:] + str(index) + side for side in ("a", "b")]
            self.run(["ip", "link", "add", ends[0], "type", "veth", "peer", "name", ends[1]])
            self.links.extend(ends)
            for host_name, side in zip(ends, ("left", "right")):
                device, interface, address = link[side]
                self.run(["ip", "link", "set", host_name, "netns", pids[device]])
                command = ["nsenter", "-t", pids[device], "-n", "ip"]
                self.run(command + ["link", "set", host_name, "name", interface])
                self.run(command + ["address", "add", address, "dev", interface])
                self.run(command + ["link", "set", interface, "up"])
        for device, config in self.case["routers"].items():
            command = ["nsenter", "-t", pids[device], "-n", "ip"]
            self.run(command + ["link", "set", "lo", "up"])
            self.run(command + ["address", "add", config["loopback"], "dev", "lo"])
            for _ in range(30):
                probe = subprocess.run(["docker", "exec", self.containers[device], "vtysh", "-c", "show version"], capture_output=True, text=True)
                if probe.returncode == 0:
                    break
                time.sleep(.5)
            else:
                raise RuntimeError(device + "的FRR进程未就绪")
            commands = ["configure terminal", "hostname " + device]
            for link in self.case["links"]:
                for side in ("left", "right"):
                    if link[side][0] == device:
                        commands += ["interface " + link[side][1], "ip ospf network point-to-point", "ip ospf hello-interval 1", "ip ospf dead-interval 4", "exit"]
            commands += ["router ospf", "ospf router-id " + config["router_id"]]
            commands += ["network " + network + " area 0.0.0.0" for network in config["networks"]]
            self.cli(device, commands + ["end"])
        self.converge()

    def converge(self):
        def full_count(value):
            if isinstance(value, dict):
                return sum(full_count(item) for item in value.values())
            if isinstance(value, list):
                return sum(full_count(item) for item in value)
            return int(isinstance(value, str) and value.startswith("Full"))
        expected = {device: sum(link[side][0] == device for link in self.case["links"] for side in ("left", "right")) for device in self.containers}
        for _ in range(40):
            states = {device: json.loads(self.cli(device, ["show ip ospf neighbor json"])) for device in self.containers}
            if all(full_count(states[device]) >= count for device, count in expected.items()):
                time.sleep(3)
                return
            time.sleep(1)
        raise RuntimeError("OSPF邻居未完成收敛，请查看commands.log")

    def collect(self):
        output = {}
        for device, name in self.containers.items():
            output[device] = {"config": self.cli(device, ["show running-config"]),
                              "neighbors": json.loads(self.cli(device, ["show ip ospf neighbor json"])),
                              "routes": json.loads(self.cli(device, ["show ip route json"])), "pings": {}}
        for goal in self.case["goals"]:
            if goal["kind"] == "ping":
                command = ["docker", "exec", self.containers[goal["device"]], "ping", "-c", str(goal.get("count", 3)), "-W", "1", "-I", goal["source_address"], goal["target"]]
                output[goal["device"]]["pings"][goal["id"]] = self.run(command, check=False)
        return output

    def close(self):
        for name in self.containers.values():
            self.run(["docker", "rm", "-f", name], check=False)
        for name in self.links:
            self.run(["ip", "link", "delete", name], check=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", type=Path, default=ROOT / "data/lab/frr_three_routers.json")
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/repair-lab")
    args = parser.parse_args()
    if platform.system() != "Linux" or os.geteuid() != 0:
        parser.error("请在安装Docker的Linux环境以sudo运行，或使用GitHub的Run FRR repair comparison工作流")
    folder = args.output.resolve()
    if not folder.is_relative_to(ROOT):
        parser.error("输出位置请放在项目内")
    folder.mkdir(parents=True, exist_ok=True)
    case = json.loads(args.case.read_text(encoding="utf-8"))
    subprocess.run(["docker", "pull", IMAGE], check=True, timeout=180)
    digest = subprocess.check_output(["docker", "image", "inspect", IMAGE, "--format", "{{index .RepoDigests 0}}"], text=True).strip()
    run = {"kind": RUN_KIND, "schema_version": 1, "run_id": uuid.uuid4().hex, "title": case["title"],
           "started_at": datetime.now(timezone.utc).isoformat(), "environment": {"engine":"FRRouting", "image": IMAGE, "image_digest": digest,
           "platform": platform.platform(), "action_url": os.environ.get("LABAGENT_ACTION_URL", "")}, "goals": case["goals"], "phases": []}
    for candidate in [{"id":"baseline", "title":"故障基线", "commands":{}}, *case["candidates"]]:
        phase_folder = folder / candidate["id"]; phase_folder.mkdir(exist_ok=True)
        lab = FrrLab(case, phase_folder)
        try:
            lab.setup()
            for device, commands in candidate["commands"].items():
                lab.cli(device, ["configure terminal", *commands, "end"])
            if candidate["commands"]:
                lab.converge()
            run["phases"].append({**candidate, "observations": lab.collect(), "captured_at": datetime.now(timezone.utc).isoformat()})
            (folder / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            lab.close()
    comparison = compare_run(run)
    (folder / "comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"run_id":run["run_id"],"phases":[{"id":p["id"],"passed":p["passed_count"],"total":p["total"]} for p in comparison["phases"]],"recommended":comparison["recommended"]},ensure_ascii=False))


if __name__ == "__main__":
    main()
