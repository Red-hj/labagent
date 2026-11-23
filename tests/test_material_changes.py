"""Saved snapshot changes preserve provenance without claiming experiment success."""
import json
import unittest

from labagent.material_changes import compare_materials


class MaterialChangesTests(unittest.TestCase):
    def test_updated_lines_keep_their_old_and_new_line_numbers(self):
        result = compare_materials({"R1.log": "header\nGE0/0/0 down\nother"}, {"R1.log": "header\nGE0/0/0 up\nother"})
        item = result["items"][0]
        self.assertEqual((item["kind"], item["added"], item["removed"]), ("updated", 1, 1))
        self.assertEqual(item["preview"], [
            {"kind": "removed", "line": 2, "text": "GE0/0/0 down"},
            {"kind": "added", "line": 2, "text": "GE0/0/0 up"}])
        self.assertNotIn("passed", result)
        self.assertNotIn("resolved", result)

    def test_first_snapshot_unchanged_additions_and_removals_are_distinct(self):
        self.assertFalse(compare_materials(None, {"R1": "up"})["has_previous"])
        self.assertFalse(compare_materials({"R1": "up"}, {"R1": "up"})["changed"])
        added = compare_materials({}, {"R1": "up"})["items"][0]
        self.assertEqual((added["kind"], added["added"], added["removed"]), ("added", 1, 0))
        removed = compare_materials({"R1": "up"}, {})["items"][0]
        self.assertEqual((removed["kind"], removed["added"], removed["removed"]), ("removed", 0, 1))

    def test_unrelated_materials_are_not_compared_as_one_device(self):
        changes = compare_materials({"R1": "down", "R2": "up"}, {"R1": "up", "R2": "up"})
        self.assertEqual([item["material"] for item in changes["items"]], ["R1"])

    def test_secret_changes_do_not_expose_old_or_new_values(self):
        result = compare_materials({"R1": "password old-private"}, {"R1": "password new-private"})
        self.assertTrue(result["items"][0]["redacted_only"])
        self.assertEqual(result["items"][0]["preview"], [])
        self.assertNotIn("private", json.dumps(result))

    def test_preview_limits_keep_full_change_counts_and_mark_truncation(self):
        result = compare_materials({}, {"R1": "\n".join(["x" * 400] + [str(i) for i in range(30)])})
        item = result["items"][0]
        self.assertEqual(item["added"], 31)
        self.assertEqual(len(item["preview"]), 12)
        self.assertEqual(len(item["preview"][0]["text"]), 300)
        self.assertTrue(item["truncated"])
