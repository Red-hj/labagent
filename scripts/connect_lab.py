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


def main():
    parser = argparse.ArgumentParser(description="让LabAgent网站采集本机已配置的MCP实验输出；Ctrl+C断开")
    parser.add_argument("config", type=Path, help="本地MCP工具映射配置，不会上传配置或设备凭据")
    parser.add_argument("--site", default="https://labagent-15yh.onrender.com")
    parser.add_argument("--check", action="store_true", help="检查本机MCP连接并列出可用检查，然后退出；无需网站连接码")
    args = parser.parse_args()
    try:
        if not args.config.is_file():
            raise MCPError(f"未找到 MCP 配置文件：{args.config}。请先按 docs/接口说明.md 配置本地 MCP 服务，"
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
        print("未读到连接码，请在终端中运行连接程序，或先用--check检查本机MCP。", file=sys.stderr)
        return 1
    except (MCPError, PermissionError, ConnectionError, OSError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
