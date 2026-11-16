#!/usr/bin/env python3
"""Track newly listed waste-factory records in the official DIW registry."""

import argparse
import csv
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

SOURCE_URL = (
    "https://diw-dataset.diw.go.th/dataset/"
    "2d778482-a081-4b1e-9803-89e35523c50c/resource/"
    "326c8b48-1b0a-4e0e-8eca-4da485abd170/download/factype3.csv"
)
TARGET_CODES = {"10100", "10500", "10600"}
REQUIRED_COLUMNS = {
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
}


def _value(row, column):
    return (row.get(column) or "").strip()


def parse_registry_csv(content):
    reader = csv.DictReader(io.StringIO(content, newline=""))
    if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(reader.fieldnames):
        raise ValueError("CSV headers do not match the expected DIW registry schema")

    records = {}
    for line_number, row in enumerate(reader, start=2):
        code = _value(row, "รหัสประเภท")
        if code not in TARGET_CODES or _value(row, "จำพวก") != "3":
            continue

        fid = _value(row, "fid")
        if not fid:
            raise ValueError(f"DIW record on CSV line {line_number} has no fid")
        if fid in records:
            raise ValueError(f"Duplicate DIW fid {fid} on CSV line {line_number}")

        year = _value(row, "ปีที่อนุญาต")
        records[fid] = {
            "fid": fid,
            "registration_number": _value(row, "เลขทะเบียนโรงงานเดิม"),
            "type_code": code,
            "type_name": _value(row, "ประเภทโรงงาน"),
            "license_year_be": int(year) if year.isdigit() else None,
            "name": _value(row, "ชื่อโรงงาน"),
            "activity": _value(row, "ประกอบกิจการ"),
            "address": {
                "number": _value(row, "เลขที่"),
                "moo": _value(row, "หมู่"),
                "soi": _value(row, "ซอย"),
                "road": _value(row, "ถนน"),
                "tambon": _value(row, "ตำบล"),
                "amphoe": _value(row, "อำเภอ"),
                "province": _value(row, "จังหวัด"),
                "postal_code": _value(row, "รหัสไปรษณีย์"),
            },
        }

    if not records:
        raise ValueError("No matching type-3 waste-factory records found in DIW CSV")
    return records


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write("\n")
    os.replace(temporary, path)


def _load_seen_ids(path):
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError(f"Unsupported registry state file: {path}")
    ids = data.get("seen_fids")
    if not isinstance(ids, list) or any(not isinstance(fid, str) or not fid for fid in ids):
        raise ValueError(f"Invalid seen_fids in registry state file: {path}")
    return set(ids)


def update_snapshot(records, state_path, reports_dir, checked_at=None):
    checked_at = checked_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    seen_ids = _load_seen_ids(state_path)
    current_ids = set(records)
    report_time = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    report_name = report_time.strftime("factory-registry-%Y%m%dT%H%M%SZ.json")

    if seen_ids is None:
        _write_json(
            reports_dir / report_name,
            {
                "checked_at": checked_at,
                "source_url": SOURCE_URL,
                "meaning": "Initial baseline; no records are classified as new.",
                "initial_baseline": True,
                "type_codes": sorted(TARGET_CODES),
                "count": 0,
                "records": [],
            },
        )
        _write_json(state_path, {"version": 1, "seen_fids": sorted(current_ids)})
        print(
            f"Initialized baseline with {len(current_ids)} records; "
            "no records reported as new."
        )
        return 0

    new_ids = current_ids - seen_ids
    if not new_ids:
        print(f"No new registry records; checked {len(current_ids)} matching records.")
        return 0

    new_records = [records[fid] for fid in sorted(new_ids)]
    report = {
        "checked_at": checked_at,
        "source_url": SOURCE_URL,
        "meaning": "Newly listed in the DIW registry; this is not construction-status data.",
        "initial_baseline": False,
        "type_codes": sorted(TARGET_CODES),
        "count": len(new_records),
        "records": new_records,
    }

    _write_json(reports_dir / report_name, report)
    _write_json(
        state_path,
        {"version": 1, "seen_fids": sorted(seen_ids | current_ids)},
    )
    print(f"Found {len(new_records)} new registry records -> {reports_dir / report_name}")
    return len(new_records)


def download_registry_csv():
    response = requests.get(SOURCE_URL, timeout=(15, 120))
    response.raise_for_status()
    return response.content.decode("utf-8-sig")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default="data/factory_registry_seen_ids.json")
    parser.add_argument("--reports-dir", default="reports/factory-registry")
    args = parser.parse_args()

    records = parse_registry_csv(download_registry_csv())
    print(f"Loaded {len(records)} matching type-3 records from the DIW registry.")
    update_snapshot(records, Path(args.state), Path(args.reports_dir))


if __name__ == "__main__":
    main()
