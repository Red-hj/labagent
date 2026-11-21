"""Keep a curated local MCP lab reachable by LabAgent through outbound HTTPS."""
import argparse
import getpass
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, ProxyHandler

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from labagent.mcp_client import LabMCPTools, MCPError, NoRedirect
from labagent.device_console import ConsoleError


class Website:
    def __init__(self, url):
        parsed = urlparse(url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/") or not parsed.netloc or not (
                parsed.scheme == "https" or parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}):
            raise MCPError("请填写LabAgent网站根地址（公网须为HTTPS）")
        self.url, self.token = url.rstrip("/"), None
        self.opener = build_opener(ProxyHandler({}), NoRedirect()) if parsed.hostname in {"127.0.0.1", "localhost", "::1"} else build_opener(NoRedirect())

    def post(self, path, data):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            with self.opener.open(Request(self.url + path, data=json.dumps(data, ensure_ascii=False).encode(), headers=headers), timeout=40) as response:
                raw = response.read(180_001)
                if len(raw) > 180_000:
                    raise MCPError("网站响应过大，请检查网站版本或本次采集状态")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise MCPError("网站响应格式无效，请确认网站地址与版本")
                return result
        except HTTPError as error:
            if error.code == 401:
                raise PermissionError("连接已失效，请在网站重新生成连接码") from None
            raise MCPError("网站拒绝了连接请求，请检查连接码、版本或本次采集状态") from None
        except MCPError:
            raise
        except ValueError:
            raise MCPError("网站未返回有效JSON，请确认网站地址与版本") from None
        except (URLError, OSError):
            raise ConnectionError("暂时无法连接LabAgent网站") from None


def execute_job(lab, website, job):
    """Do not replay a command if result delivery fails after it executed."""
    if not isinstance(job, dict) or not isinstance(job.get("id"), str):
        raise MCPError("网站未返回有效采集请求")
    try:
        if not isinstance(job.get("tool"), str) or job["tool"] not in lab.bindings:
            raise MCPError("网站请求的检查未在本机开放")
        result = lab.execute(job["tool"], job.get("arguments", {}))
    except (MCPError, ValueError, KeyError):
        website.post("/api/lab/agent/result", {"id": job.get("id"), "error": "observation_failed"})
        return False
    website.post("/api/lab/agent/result", {"id": job["id"], "result": result})
    return True


def configure_console(path):
    from labagent.device_console import validate_devices
    path = path.resolve()
    if not path.is_relative_to(ROOT / "runtime"):
        raise MCPError("请将新配置保存在项目 runtime 目录，例如 runtime/mcp-ensp.json。")
    devices_path = path.with_name(path.stem + ".devices.json")
    if path.exists() or devices_path.exists():
        raise MCPError("配置文件已经存在。可直接运行 --probe 检查；需要另一份配置时，请在命令中指定新的 runtime 文件名。")
    print("配置 Huawei VRP 实验设备的 Telnet 控制台。", flush=True)
    print("先启动模拟设备，并在模拟器控制台完成首次登录，确认出现 <R1> 或 <Huawei> 等提示符。", flush=True)
    print("请按模拟器显示的控制台端口填写；主机地址默认为本机。", flush=True)
    devices = {}
    while len(devices) < 16:
        name = input("设备名称（例如 R1；直接回车结束）：").strip()
        if not name:
            break
        host = input("控制台主机（默认 127.0.0.1）：").strip() or "127.0.0.1"
        try:
            port = int(input("控制台端口：").strip())
        except ValueError:
            raise MCPError("请填写模拟器中的实际控制台端口数字。") from None
        if name in devices:
            raise MCPError("设备名称重复，请给每台设备使用不同名称。")
        devices[name] = {"host": host, "port": port}
        # Validate each entry before generating either file.
        validate_devices({"devices": devices})
    normalized = validate_devices({"devices": devices})
    spec = {"command": sys.executable, "args": ["-B", "scripts/device_mcp.py",
        devices_path.relative_to(ROOT).as_posix()], "cwd": ".", "timeout": 25, "tools": {}}
    for name, device in normalized.items():
        spec["tools"][name.lower()] = {"name": "device_exec", "effect": "observe", "device": name,
            "description": f"读取实验设备 {name} 的接口、路由、邻居与配置输出",
            "arguments": {"target": name}, "choices": {"command": device["commands"]}}
    path.parent.mkdir(parents=True, exist_ok=True)
    with devices_path.open("x", encoding="utf-8") as target:
        json.dump({"devices": normalized}, target, ensure_ascii=False, indent=2)
    with path.open("x", encoding="utf-8") as target:
        json.dump({"servers": {"console": spec}}, target, ensure_ascii=False, indent=2)
    print(f"本地配置已保存：{path.relative_to(ROOT)}", flush=True)
    print(f"接下来运行：python scripts/connect_lab.py {path.relative_to(ROOT).as_posix()} --probe", flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description="让LabAgent网站采集本机已配置的MCP实验输出；Ctrl+C断开")
    parser.add_argument("config", type=Path, nargs="?", default=Path("runtime/mcp-ensp.json"), help="本地MCP工具映射配置，不会上传配置或设备凭据")
    parser.add_argument("--site", default="https://labagent-15yh.onrender.com")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--setup", action="store_true", help="交互生成内置 Huawei VRP 控制台配置")
    mode.add_argument("--probe", action="store_true", help="每台设备实际读取第一项检查，确认设备输出；无需连接码")
    mode.add_argument("--check", action="store_true", help="检查本机MCP连接并列出可用检查，然后退出；无需网站连接码")
    args = parser.parse_args()
    if not args.config.is_absolute():
        args.config = ROOT / args.config
    try:
        if args.setup:
            return configure_console(args.config)
        if not args.config.is_file():
            raise MCPError(f"未找到 MCP 配置文件：{args.config}。Huawei VRP 控制台可先运行 python scripts/connect_lab.py --setup；其他 MCP 服务按 docs/接口说明.md 配置。"
                           "可参考 data/examples/mcp-ensp.example.json；再把命令中的路径替换为实际配置文件。")
        with LabMCPTools(args.config) as lab:
            for warning in lab.warnings:
                print("提示：" + warning, file=sys.stderr, flush=True)
            if not lab.tools:
                raise MCPError("没有可用检查，请先用scripts/mcp_lab.py检查本地MCP配置")
            print("将向所选网站开放以下检查，设备凭据留在本机：", flush=True)
            for tool in lab.tools:
                fn = tool["function"]
                print("- " + fn["name"] + "：" + fn["description"], flush=True)
                for name, schema in fn["parameters"]["properties"].items():
                    print("  " + name + "：" + " / ".join(schema["enum"]), flush=True)
            if args.check:
                print("本机MCP自检通过，已发现上述检查；设备输出将在网页发起采集后读取。", flush=True)
                return 0
            if args.probe:
                for tool in lab.tools:
                    fn = tool["function"]
                    choices = {name: definition["enum"][0] for name, definition in fn["parameters"]["properties"].items()}
                    print("正在实际读取：" + fn["description"], flush=True)
                    try:
                        observed = lab.execute(fn["name"], choices)
                    except MCPError as error:
                        raise MCPError(str(error) + "。请检查设备启动、控制台端口，以及是否已完成首次登录。") from None
                    print(observed["device"] + " · " + observed["command"], flush=True)
                    print(observed["text"], flush=True)
                print("设备读取通过。运行相同命令并去掉 --probe，即可输入网页连接码并保持在线。", flush=True)
                return 0
            website = Website(args.site)
            print("请在网站登录个人账号，打开‘连接我的实验电脑’生成连接码。", flush=True)
            code = getpass.getpass("连接码（输入隐藏）：").strip()
            grant = website.post("/api/lab/agent/claim", {"code": code, "tools": lab.tools})
            code = ""
            token = grant.get("token")
            if not isinstance(token, str) or not token:
                raise MCPError("网站未返回有效连接令牌，请确认网站版本后重新配对")
            website.token = token
            print("已连接。回到网页选择检查；Ctrl+C停止。最长8小时，网站重启后须重新配对。", flush=True)
            failures = 0
            while True:
                try:
                    job = website.post("/api/lab/agent/poll", {}).get("job")
                    if job:
                        print("正在读取一次设备输出…", flush=True)
                        try:
                            success = execute_job(lab, website, job)
                            print("输出已送回网页。" if success else "设备检查失败，请检查本地MCP或设备状态。", flush=True)
                        except (ConnectionError, MCPError):
                            print("本次输出未送达，未重复执行命令；请在网页手动重试。", flush=True)
                    failures = 0
                except ConnectionError:
                    failures += 1
                    if failures >= 3:
                        raise MCPError("网站持续不可达，已停止连接。请确认网络后重新连接") from None
                    print("网络暂时不可达，正在恢复连接…", flush=True)
                    time.sleep(2)
    except KeyboardInterrupt:
        print("\n本地连接已停止。网页将在稍后显示离线。")
    except EOFError:
        print("未读到设备配置，请在终端中运行 --setup。" if args.setup else "未读到连接码，请在终端中运行连接程序，或先用 --check 检查本机 MCP。", file=sys.stderr)
        return 1
    except (MCPError, ConsoleError, PermissionError, ConnectionError, OSError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
