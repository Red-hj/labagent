"""Read operator-selected Huawei VRP commands over a simulator's Telnet console.

The console transport follows RFC 854. Device addresses and command choices stay
in local configuration; this module never accepts a target address from a model.
"""
import json
from pathlib import Path
import re
import socket
import time

DEFAULT_COMMANDS = [
    "display ip interface brief", "display interface brief", "display ip routing-table",
    "display arp", "display vlan", "display lldp neighbor brief",
    "display ospf peer", "display ospf interface", "display current-configuration",
]
PROMPT = re.compile(r"(?:^|\n)(<[^<>\r\n]{1,80}>|\[[^\[\]\r\n]{1,80}\])[ \t]*\Z")
MORE = re.compile(r"-{2,}\s*More\s*-{2,}", re.IGNORECASE)
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
AUTH = re.compile(r"(?:password|username|login|密码|用户名)\s*[:：]\s*$", re.IGNORECASE)
MAX_OUTPUT = 96_000


class ConsoleError(ValueError):
    pass


def validate_devices(settings):
    devices = settings.get("devices") if isinstance(settings, dict) else None
    if not isinstance(devices, dict) or not 1 <= len(devices) <= 16:
        raise ConsoleError("请配置 1 至 16 台设备的控制台地址。")
    normalized, aliases = {}, set()
    for name, spec in devices.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,24}", name):
            raise ConsoleError("设备名称请使用字母开头的字母、数字或下划线，例如 R1。")
        if name.lower() in aliases:
            raise ConsoleError("设备名称不能仅大小写不同。")
        aliases.add(name.lower())
        if not isinstance(spec, dict):
            raise ConsoleError("设备配置必须包含 host 和 port。")
        host, port = spec.get("host"), spec.get("port")
        if not isinstance(host, str) or not host or len(host) > 253 or any(c.isspace() for c in host):
            raise ConsoleError("请填写实际控制台主机地址。")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ConsoleError("控制台端口必须是 1 至 65535 的整数。")
        commands = spec.get("commands", DEFAULT_COMMANDS)
        if not isinstance(commands, list) or not commands or len(commands) > 30 or any(
                not isinstance(cmd, str) or cmd != cmd.strip() or not re.fullmatch(
                    r"display [A-Za-z0-9][A-Za-z0-9 _./:-]{0,190}", cmd) for cmd in commands):
            raise ConsoleError("请配置单条 display 检查命令，例如 display ip interface brief。")
        normalized[name] = {"host": host, "port": port, "commands": list(dict.fromkeys(commands))}
    return normalized


def load_devices(path):
    try:
        return validate_devices(json.loads(Path(path).read_text(encoding="utf-8-sig")))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConsoleError("无法读取设备配置，请先运行 scripts/connect_lab.py --setup。") from None


class TelnetStream:
    """Parse fragmented IAC commands and pass only terminal data to the reader."""
    def __init__(self, send):
        self.send, self.mode = send, "data"
        self.remote, self.local = set(), set()

    def negotiate(self, command, option):
        # Echo and suppress-go-ahead are sufficient for a noninteractive console.
        # Acknowledge state changes only, avoiding positive negotiation loops.
        if command == 251:  # WILL
            if option in {1, 3}:
                if option not in self.remote:
                    self.remote.add(option)
                    self.send(bytes((255, 253, option)))
            else:
                self.send(bytes((255, 254, option)))
        elif command == 253:  # DO
            if option == 3:
                if option not in self.local:
                    self.local.add(option)
                    self.send(bytes((255, 251, option)))
            else:
                self.send(bytes((255, 252, option)))
        elif command == 252 and option in self.remote:  # WONT
            self.remote.remove(option)
            self.send(bytes((255, 254, option)))
        elif command == 254 and option in self.local:  # DONT
            self.local.remove(option)
            self.send(bytes((255, 252, option)))

    def feed(self, raw):
        data = bytearray()
        for value in raw:
            if self.mode == "data":
                if value == 255:
                    self.mode = "iac"
                else:
                    data.append(value)
            elif self.mode == "iac":
                if value == 255:
                    data.append(value)
                    self.mode = "data"
                elif value in {251, 252, 253, 254}:
                    self.mode = value
                else:
                    self.mode = "sub" if value == 250 else "data"
            elif self.mode == "sub":
                if value == 255:
                    self.mode = "sub_iac"
            elif self.mode == "sub_iac":
                self.mode = "data" if value == 240 else "sub"
            else:
                self.negotiate(self.mode, value)
                self.mode = "data"
        return bytes(data)


def terminal_text(raw):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gb18030", errors="replace")
    text = ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "").replace("\x00", "").replace("\x07", "")
    output = []
    for char in text:
        if char == "\b":
            if output and output[-1] != "\n":
                output.pop()
        else:
            output.append(char)
    return "".join(output)


def _read_prompt(connection, stream, deadline, *, prompt=None):
    output, awakened, last_page = bytearray(), False, -1
    while time.monotonic() < deadline:
        connection.settimeout(min(.25, max(.001, deadline - time.monotonic())))
        try:
            raw = connection.recv(4096)
        except socket.timeout:
            if prompt is None and not awakened:
                connection.sendall(b"\r\n")
                awakened = True
            continue
        if not raw:
            raise ConsoleError("控制台在返回完整输出前已关闭，请检查设备状态。")
        output.extend(stream.feed(raw))
        if len(output) > MAX_OUTPUT:
            raise ConsoleError("设备输出超过本次采集大小，请选择范围更小的 display 命令。")
        text = terminal_text(output)
        if prompt is None and AUTH.search(text):
            raise ConsoleError("控制台正在等待登录或首次密码设置，请先在模拟器中完成操作并进入设备命令提示符。")
        found = PROMPT.search(text)
        if found and found[1] != "[Y/N]" and (prompt is None or found[1] == prompt):
            return text, found
        pages = list(MORE.finditer(text))
        page = pages[-1] if pages else None
        if prompt is not None and page and not text[page.end():].strip() and last_page != page.start():
            connection.sendall(b" ")
            last_page = page.start()
    raise ConsoleError("等待设备命令提示符超时，请确认设备已启动且控制台可用。")


def read_command(device, command, *, timeout=18):
    if command not in device["commands"]:
        raise ConsoleError("本台设备未开放这个检查命令。")
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((device["host"], device["port"]), timeout=timeout) as connection:
            stream = TelnetStream(connection.sendall)
            _, initial = _read_prompt(connection, stream, deadline)
            prompt = initial[1]
            connection.sendall(command.encode("ascii") + b"\r\n")
            text, final = _read_prompt(connection, stream, deadline, prompt=prompt)
    except OSError:
        raise ConsoleError("无法读取控制台，请检查设备是否启动，以及控制台地址和端口是否正确。") from None
    body = MORE.sub("", text[:final.start()]).strip()
    lines = body.splitlines()
    if lines and lines[0].strip() in {command, prompt + command}:
        body = "\n".join(lines[1:]).strip()
    if not body:
        raise ConsoleError("设备没有返回命令输出，请在控制台核对这个检查命令。")
    if re.search(r"(?:^|\n)\s*(?:Error:|%\s*(?:Invalid|Unknown|Unrecognized))", body, re.IGNORECASE):
        raise ConsoleError("设备拒绝了这条检查命令，请确认当前设备型号和命令是否适用。")
    return body
