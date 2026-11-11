"""Parse a bounded subset of Huawei VRP text configuration, preserving lines."""
from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import re


def area_id(value: str) -> str:
    return str(ipaddress.IPv4Address(int(value) if value.isdecimal() else value))


def interface_id(value: str) -> str:
    value = value.lower().replace(" ", "")
    return re.sub(r"^(gigabitethernet|ge)(?=\d)", "ge", value)


@dataclass
class Interface:
    name: str
    address: str | None = None
    mask: str | None = None
    shutdown: bool = False
    direct_ospf: tuple[int, str] | None = None
    hello: int = 10
    dead: int = 40
    lines: dict = field(default_factory=dict)
    unsupported: list = field(default_factory=list)

    @property
    def network(self):
        if self.address and self.mask:
            return ipaddress.IPv4Interface(f"{self.address}/{self.mask}").network
        return None


@dataclass
class Process:
    number: int
    router_id: str | None = None
    networks: list = field(default_factory=list)
    silent: set = field(default_factory=set)
    unsupported: list = field(default_factory=list)
    lines: dict = field(default_factory=dict)
    area_lines: dict = field(default_factory=dict)


@dataclass
class Device:
    name: str
    interfaces: dict = field(default_factory=dict)
    processes: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)


def parse_config(name: str, text: str) -> Device:
    device = Device(name)
    interface = process = area = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("#") or line == "return":
            interface = process = area = None
            continue
        if line == "quit":
            if interface is not None:
                interface = None
            elif area is not None:
                area = None
            else:
                process = None
            continue
        evidence = {"device": name, "line": number, "text": line}
        try:
            match = re.fullmatch(r"interface\s+(.+)", line, re.I)
            if match:
                key = interface_id(match[1])
                interface = device.interfaces.setdefault(key, Interface(match[1]))
                interface.lines["interface"] = evidence
                process = area = None
                continue
            match = re.fullmatch(r"ospf\s+(\d+)(?:\s+router-id\s+(\S+))?", line, re.I)
            if match:
                process = device.processes.setdefault(int(match[1]), Process(int(match[1])))
                if match[2]:
                    process.router_id = str(ipaddress.IPv4Address(match[2]))
                    process.lines["router_id"] = evidence
                interface = area = None
                continue
            if interface is not None:
                match = re.fullmatch(r"ip address\s+(\S+)\s+(\S+)", line, re.I)
                if match:
                    parsed = ipaddress.IPv4Interface(f"{match[1]}/{match[2]}")
                    interface.address, interface.mask = str(parsed.ip), str(parsed.netmask)
                    interface.lines["ip"] = evidence
                elif line in ("shutdown", "undo shutdown"):
                    interface.shutdown = line == "shutdown"
                    interface.lines["state"] = evidence
                elif match := re.fullmatch(r"ospf enable\s+(\d+)\s+area\s+(\S+)", line, re.I):
                    interface.direct_ospf = (int(match[1]), area_id(match[2]))
                    interface.lines["ospf"] = evidence
                elif match := re.fullmatch(r"ospf timer (hello|dead)\s+(\d+)", line, re.I):
                    if int(match[2]) < 1:
                        raise ValueError("定时器必须大于零")
                    setattr(interface, match[1].lower(), int(match[2]))
                    interface.lines[match[1].lower()] = evidence
                elif line.startswith(("ospf ", "undo ospf", "ip address", "undo ip address", "ip unnumbered")):
                    interface.unsupported.append(evidence)
            elif process is not None:
                if match := re.fullmatch(r"area\s+(\S+)", line, re.I):
                    area = area_id(match[1])
                    process.area_lines[area] = evidence
                elif match := re.fullmatch(r"network\s+(\S+)\s+(\S+)", line, re.I):
                    if area is None:
                        raise ValueError("network 缺少所属 area")
                    address = int(ipaddress.IPv4Address(match[1]))
                    wildcard = int(ipaddress.IPv4Address(match[2]))
                    process.networks.append((address, wildcard, area, {**evidence, "area_evidence": process.area_lines[area]}))
                elif match := re.fullmatch(r"silent-interface\s+(.+)", line, re.I):
                    process.silent.add(interface_id(match[1]))
                    process.lines["silent:" + interface_id(match[1])] = evidence
                elif not line.startswith(("description ", "preference ", "bandwidth-reference ")):
                    process.unsupported.append(evidence)
        except ValueError as error:
            device.errors.append({**evidence, "error": str(error)})
    return device


def ospf_bindings(device: Device, interface: Interface):
    """Return all effective process/area candidates; ambiguity is kept explicit."""
    if interface.direct_ospf is not None:
        return [(interface.direct_ospf[0], interface.direct_ospf[1], interface.lines["ospf"])]
    if not interface.address:
        return []
    address = int(ipaddress.IPv4Address(interface.address))
    bindings = []
    for process in device.processes.values():
        for base, wildcard, area, evidence in process.networks:
            mask = 0xFFFFFFFF ^ wildcard
            if address & mask == base & mask:
                candidate = (process.number, area, evidence)
                if not any(old[:2] == candidate[:2] for old in bindings):
                    bindings.append(candidate)
    return bindings
