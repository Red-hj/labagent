"""Read Ping observations from VRP, Windows and Linux text, preserving evidence."""
import ipaddress
import re

IP = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
NUMBER = r"(?<![\d-])(\d{1,8})(?!\d)"
SENT = re.compile(NUMBER + r"\s+packet(?:s|\(s\))?\s+transmitted", re.I)
RECEIVED = re.compile(NUMBER + r"\s+(?:packet(?:s|\(s\))?\s+)?received", re.I)
WINDOWS = re.compile(r"(?:Sent|(?:已)?发送)\s*[=:：]?\s*(\d{1,8})(?!\d).*?(?:Received|(?:已)?接收|收到)\s*[=:：]?\s*(\d{1,8})(?!\d)", re.I)


def parse_ping_records(lines):
    """Each output block owns its target and counters; never combine blocks."""
    records, current = [], None
    for number, text in enumerate(lines, 1):
        address = IP.search(text) if re.search(r"(?<![a-z])ping(?![a-z])", text, re.I) else None
        if address:
            try:
                target = str(ipaddress.IPv4Address(address[0]))
            except ValueError:
                current = None
                continue
            # Repeated headings (command, response header, statistics) belong
            # to one block until its counters have been emitted.
            command = re.search(r"^\s*(?:<[^>]+>|\[[^\]]+\]|\S+[>$#])\s*ping\b", text, re.I)
            fresh_output = current and ("sent" in current or "received" in current) and not re.search(r"statistics|统计", text, re.I)
            if current is None or current["target"] != target or current.get("complete") or command or fresh_output:
                current = {"target": target, "evidence": []}
            current["evidence"].append({"line": number, "quote": text})
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
