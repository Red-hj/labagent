"""Read Ping observations from VRP, Windows and Linux text, preserving evidence."""
import ipaddress
import re

IP = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
PING_WORD = re.compile(r"(?<![a-z])ping(?:ing)?(?![a-z])", re.I)
PING_COMMAND = re.compile(r"^\s*(?P<prompt><[^>]+>|\[[^\]]+\]|\S+[>$#])\s*ping\b", re.I)
PING_STATISTICS = re.compile(r"\bstatistics\b|统计", re.I)
LINUX_PING_SOURCE = re.compile(r"(?<!\S)-I[ \t]*\S+")
VRP_PING_SOURCE = re.compile(r"(?<!\S)-a[ \t]+" + IP.pattern)
ADDRESS_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_.])(?P<address>(?:[0-9]{1,3}\.){3}[0-9]{1,3}|(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4})"
    r"(?P<prefix>/[0-9]{1,3})?(?![0-9a-z:/]|\.[0-9])", re.I
)
PING_ARGUMENTS = re.compile(
    r"(?<![A-Za-z0-9])ping6?(?![A-Za-z0-9])[ \t]*(?:到|至)?[ \t]*[`(]?"
    r"(?:[A-Za-z0-9:._%/\-]+[ \t]*)+", re.I
)
MISSING_PING_IP = "<目标端点实际 IP>"
NUMBER = r"(?<![\d-])(\d{1,8})(?!\d)"
SENT = re.compile(NUMBER + r"\s+packet(?:s|\(s\))?\s+transmitted", re.I)
RECEIVED = re.compile(NUMBER + r"\s+(?:packet(?:s|\(s\))?\s+)?received", re.I)
WINDOWS = re.compile(r"(?:Sent|(?:已)?发送)\s*[=:：]?\s*(\d{1,8})(?!\d).*?(?:Received|(?:已)?接收|收到)\s*[=:：]?\s*(\d{1,8})(?!\d)", re.I)


def address_literal(value):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def host_literals(text):
    for match in ADDRESS_TOKEN.finditer(text):
        address = address_literal(match["address"])
        if address is None:
            continue
        if match["prefix"]:
            try:
                interface = ipaddress.ip_interface(match[0])
            except ValueError:
                continue
            if interface.network.num_addresses > 1 and interface.ip == interface.network.network_address:
                continue
        yield address


def ground_ping_text(text, known, proposed=()):
    """Remove unsupported Ping literals without changing source quotes or routing commands."""
    def replace(match):
        address = address_literal(match["address"])
        if address in known and not match["prefix"]:
            return match[0]
        return MISSING_PING_IP

    revised = PING_ARGUMENTS.sub(lambda match: ADDRESS_TOKEN.sub(replace, match[0]), text)
    if proposed:
        revised = ADDRESS_TOKEN.sub(lambda match: replace(match) if not match["prefix"] and
                                    address_literal(match["address"]) in proposed else match[0], revised)
    return revised


def _ping_address(text, mention, command, statistics):
    arguments = text[mention.end():]
    if statistics:
        return IP.search(arguments) or IP.search(text[:mention.start()])
    arguments = LINUX_PING_SOURCE.sub("", arguments)
    source = VRP_PING_SOURCE.search(arguments)
    # The VRP prompt or a second address distinguishes -a from the flag in host tools.
    if source and ((command and command["prompt"].startswith(("<", "[")))
                   or IP.search(arguments[source.end():])):
        arguments = arguments[:source.start()] + arguments[source.end():]
    return IP.search(arguments)


def parse_ping_records(lines):
    """Each output block owns its target and counters; never combine blocks."""
    records, current = [], None
    for number, text in enumerate(lines, 1):
        command = PING_COMMAND.match(text)
        mention = command or PING_WORD.search(text)
        statistics = bool(mention and not command and PING_STATISTICS.search(text))
        address = _ping_address(text, mention, command, statistics) if mention else None
        if address:
            try:
                target = str(ipaddress.IPv4Address(address[0]))
            except ValueError:
                current = None
                continue
            # Repeated headings (command, response header, statistics) belong
            # to one block until its counters have been emitted.
            fresh_output = current and ("sent" in current or "received" in current) and not statistics
            if current is None or current["target"] != target or current.get("complete") or command or fresh_output:
                current = {"target": target, "evidence": []}
            current["evidence"].append({"line": number, "quote": text})
        elif mention and (command or text[mention.end():].lstrip().startswith("-")):
            current = None
        if current is None or current.get("complete"):
            continue
        combined = WINDOWS.search(text)
        sent, received = SENT.search(text), RECEIVED.search(text)
        if combined:
            current.update(sent=int(combined[1]), received=int(combined[2]))
        else:
            if sent:
                current["sent"] = int(sent[1])
            if received:
                current["received"] = int(received[1])
        if (combined or sent or received) and (not current["evidence"] or current["evidence"][-1]["line"] != number):
            current["evidence"].append({"line": number, "quote": text})
        if "sent" not in current or "received" not in current:
            continue
        current["complete"] = True
        sent_count, received_count = current["sent"], current["received"]
        if not 0 < sent_count <= 1_000_000 or not 0 <= received_count <= sent_count:
            continue
        evidence = current["evidence"]
        records.append({"target": current["target"], "sent": sent_count, "received": received_count,
                        "passed": sent_count == received_count, "line": evidence[0]["line"],
                        "quote": evidence[0]["quote"], "line_end": evidence[-1]["line"], "evidence": evidence})
    return records
