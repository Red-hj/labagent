"""Describe saved material changes without turning a diff into a diagnosis."""
from difflib import SequenceMatcher

from .investigation import safe_text


def compare_materials(before, after):
    if before is None:
        return {"has_previous": False, "changed": False, "items": []}
    items = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name, ""), after.get(name, "")
        if old == new and (name in before) == (name in after):
            continue
        left, right = safe_text(old).splitlines(), safe_text(new).splitlines()
        preview, added, removed, truncated = [], 0, 0, False
        for kind, i, j, k, end in SequenceMatcher(a=left, b=right).get_opcodes():
            if kind == "equal":
                continue
            removed += j - i
            added += end - k
            for change, rows, start, stop in (("removed", left, i, j), ("added", right, k, end)):
                for index in range(start, stop):
                    if len(preview) < 12:
                        row = rows[index]
                        preview.append({"kind": change, "line": index + 1, "text": row[:300]})
                        truncated |= len(row) > 300
                    else:
                        truncated = True
        items.append({"material": name, "kind": "added" if name not in before else "removed" if name not in after else "updated",
            "added": added, "removed": removed, "preview": preview, "truncated": truncated,
            "redacted_only": left == right and old != new})
    return {"has_previous": True, "changed": bool(items), "items": items}
