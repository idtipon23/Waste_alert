import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from factory_registry_tracker import parse_registry_csv, update_snapshot


HEADERS = [
    "เลขทะเบียนโรงงานเดิม",
    "fid",
    "รหัสประเภท",
    "ประเภทโรงงาน",
    "จำพวก",
    "ปีที่อนุญาต",
    "ชื่อโรงงาน",
    "ประกอบกิจการ",
    "เลขที่",
    "หมู่",
    "ซอย",
    "ถนน",
    "ตำบล",
    "อำเภอ",
    "จังหวัด",
    "รหัสไปรษณีย์",
]


def make_csv(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=HEADERS)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def factory(fid, code="10500", factory_class="3", name="โรงงานทดสอบ"):
    return {
        "เลขทะเบียนโรงงานเดิม": "3-105-1/69",
        "fid": fid,
        "รหัสประเภท": code,
        "ประเภทโรงงาน": "โรงงานคัดแยกหรือฝังกลบ",
        "จำพวก": factory_class,
        "ปีที่อนุญาต": "2569",
        "ชื่อโรงงาน": name,
        "ประกอบกิจการ": "คัดแยกวัสดุ",
        "เลขที่": "1",
        "หมู่": "2",
        "ซอย": "",
        "ถนน": "",
        "ตำบล": "ตัวอย่าง",
        "อำเภอ": "ตัวอย่าง",
        "จังหวัด": "ตัวอย่าง",
        "รหัสไปรษณีย์": "10000",
    }


class FactoryRegistryTrackerTests(unittest.TestCase):
    def test_parser_filters_to_supported_type_three_codes(self):
        content = make_csv(
            [
                factory("a"),
                factory("b", code="01001"),
                factory("c", factory_class="2"),
            ]
        )

        records = parse_registry_csv(content)

        self.assertEqual(list(records), ["a"])
        self.assertNotIn("lat", records["a"])

    def test_first_run_seeds_baseline_without_claiming_records_are_new(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = root / "state.json"
            reports = root / "reports"

            count = update_snapshot(
                {"a": {"fid": "a"}, "b": {"fid": "b"}},
                state,
                reports,
                checked_at="2026-11-17T00:00:00Z",
            )

            self.assertEqual(count, 0)
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["seen_fids"], ["a", "b"])
            baseline_path = reports / "factory-registry-20261117T000000Z.json"
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            self.assertTrue(baseline["initial_baseline"])
            self.assertEqual(baseline["records"], [])

    def test_new_ids_are_reported_once_and_added_to_baseline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = root / "state.json"
            reports = root / "reports"
            state.write_text(json.dumps({"version": 1, "seen_fids": ["old"]}), encoding="utf-8")

            count = update_snapshot(
                {"old": {"fid": "old"}, "new": {"fid": "new"}},
                state,
                reports,
                checked_at="2026-11-17T00:00:00Z",
            )

            report_path = reports / "factory-registry-20261117T000000Z.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(count, 1)
            self.assertEqual([item["fid"] for item in report["records"]], ["new"])
            self.assertIn("not construction-status data", report["meaning"])
            self.assertEqual(
                json.loads(state.read_text(encoding="utf-8"))["seen_fids"],
                ["new", "old"],
            )


if __name__ == "__main__":
    unittest.main()
