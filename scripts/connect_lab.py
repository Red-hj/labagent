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
                return json.loads(response.read(180_000))
        except HTTPError as error:
            if error.code == 401:
                raise PermissionError("连接已失效，请在网站重新生成连接码") from None
            raise MCPError("网站拒绝了连接请求，请检查连接码、版本或本次采集状态") from None
        except (URLError, OSError, ValueError):
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
    args = parser.parse_args()
    try:
        website = Website(args.site)
        with LabMCPTools(args.config) as lab:
            if not lab.tools:
                raise MCPError("没有可用检查，请先用scripts/mcp_lab.py检查本地MCP配置")
            print("将向所选网站开放以下检查，设备凭据留在本机：", flush=True)
            for tool in lab.tools:
                print("- " + tool["function"]["description"], flush=True)
            print("请在网站登录个人账号，打开‘连接我的实验电脑’生成连接码。", flush=True)
            code = getpass.getpass("连接码（输入隐藏）：").strip()
            website.token = website.post("/api/lab/agent/claim", {"code": code, "tools": lab.tools})["token"]
            code = ""
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
    except (MCPError, PermissionError, ConnectionError, OSError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
