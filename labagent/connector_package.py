"""A small, explicit source bundle for the student's experiment computer."""
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "connect.cmd",
    "labagent/__init__.py",
    "labagent/device_console.py",
    "labagent/mcp_client.py",
    "scripts/device_mcp.py",
    "scripts/connect_lab.py",
)
INSTRUCTIONS = """LabAgent 实验电脑连接程序

准备：Python 3.10 或更新版本；已启动的 Huawei VRP 实验设备。
解压整个连接包后，在 Windows 双击 connect.cmd。

首次运行会询问设备名称、控制台主机和端口，并实际读取接口输出。
请按模拟器显示的控制台地址填写；先在模拟器中完成设备首次登录。
设备配置保存在解压目录的 runtime 文件夹中。
控制台需要等待用户名或密码时，请先在模拟器中完成登录；采集时暂停在同一控制台输入命令。

设备读取成功后，在 LabAgent 网站登录个人账号，生成连接码，
回到此程序按提示输入。保持程序运行，在网页刷新连接并选择检查。
Ctrl+C 停止连接；网站重新部署后需要重新配对。

命令行（在解压目录运行）：
  python scripts/connect_lab.py --setup   生成首次配置
  python scripts/connect_lab.py --check   只检查工具列表
  python scripts/connect_lab.py --probe   每台设备实际读取第一项检查
  python scripts/connect_lab.py           输入连接码并保持在线
已有 MCP 配置可在命令中指定其路径；其他站点用 --site 指定 HTTPS 根地址。

当前内置适配 Huawei VRP 的 Telnet 控制台。
"""


def build_connector_package():
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as package:
        for name in FILES:
            package.writestr(name, (ROOT / name).read_bytes())
        package.writestr("README.txt", INSTRUCTIONS.encode("utf-8"))
    return output.getvalue()
