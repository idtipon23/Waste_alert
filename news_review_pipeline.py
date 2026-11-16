#!/usr/bin/env python3
"""Collect reviewed news candidates and publish only explicitly approved items."""

import argparse
import hashlib
import json
import os
import tempfile
from email.utils import parsedate_to_datetime
from pathlib import Path

import monitor_core

ROOT = Path(__file__).resolve().parent
QUEUE_PATH = ROOT / "data" / "news_review_queue.json"
RESULTS_PATH = ROOT / "results_v33.jsonl"
INCIDENTS_PATH = ROOT / "web" / "data" / "incidents.json"
FACTORIES_PATH = ROOT / "factories_v3.json"
VALID_STATUSES = {"pending", "approved", "rejected"}


def _read_json(path, default=None):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as output:
        json.dump(data, output, ensure_ascii=False, separators=(",", ":"))
        output.write("\n")
    os.replace(temporary, path)


def _queue_data(path):
    data = _read_json(path, {"version": 1, "records": []})
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError(f"Unsupported news review queue: {path}")
    records = data.get("records")
    if not isinstance(records, list):
        raise ValueError(f"Invalid records in news review queue: {path}")
    for record in records:
        if (
            not isinstance(record, dict)
            or record.get("status") not in VALID_STATUSES
            or not isinstance(record.get("source_url"), str)
            or not record["source_url"].startswith(("https://", "http://"))
            or not isinstance(record.get("place"), dict)
            or (
                "published" in record
                and not isinstance(record["published"], bool)
            )
        ):
            raise ValueError(f"Invalid review record in news review queue: {path}")
    return data


def _event_date(candidate, news):
    event_date = candidate.get("event_date")
    if isinstance(event_date, str) and event_date:
        return event_date
    published = news.get("published")
    if published:
        try:
            return parsedate_to_datetime(published).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _to_review_record(candidate):
    news = candidate.get("news") or {}
    source_url = news.get("resolved_url") or news.get("link")
    place = candidate.get("place")
    if (
        not candidate.get("relevant")
        or candidate.get("scope") == "national"
    ):
        return None
    if not isinstance(source_url, str) or not source_url.startswith(("https://", "http://")):
        raise ValueError("Relevant news candidate has an invalid source URL")
    if not isinstance(place, dict):
        raise ValueError("Relevant news candidate has an invalid place")
    if not any(place.get(key) for key in ("tambon", "amphoe", "province")):
        return None

    return {
        "id": hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:16],
        "status": "pending",
        "published": False,
        "source_url": source_url,
        "source_name": news.get("source"),
        "title": (news.get("title") or "")[:200],
        "summary": candidate.get("summary_th"),
        "place": {
            key: place.get(key)
            for key in ("tambon", "amphoe", "province")
            if place.get(key)
        },
        "scope": candidate.get("scope"),
        "precision": candidate.get("location_precision"),
        "substances": candidate.get("substances") or [],
        "tags": candidate.get("tags") or [],
        "event_date": _event_date(candidate, news),
    }


def collect_candidates(results_path=RESULTS_PATH, queue_path=QUEUE_PATH, incidents_path=INCIDENTS_PATH):
    queue = _queue_data(queue_path)
    incidents = _read_json(incidents_path, [])
    known_urls = {
        record.get("source_url")
        for record in queue["records"]
        if record.get("source_url")
    }
    known_urls.update(
        record.get("source_url")
        for record in incidents
        if record.get("source_url")
    )

    added = []
    with Path(results_path).open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at line {line_number}") from exc
            record = _to_review_record(candidate)
            if record and record["source_url"] not in known_urls:
                added.append(record)
                known_urls.add(record["source_url"])

    if added:
        queue["records"].extend(added)
        _write_json(queue_path, queue)
    print(f"Added {len(added)} news candidates to the review queue.")
    action_output = os.environ.get("GITHUB_OUTPUT")
    if action_output:
        with open(action_output, "a", encoding="utf-8") as output:
            output.write(f"added={len(added)}\n")
    return len(added)


def _public_incident(record, incident_id, db):
    place = record["place"]
    coords = monitor_core.place_to_coords(
        db,
        province=place.get("province"),
        amphoe=place.get("amphoe"),
        tambon=place.get("tambon"),
    )
    if coords is None:
        raise ValueError(
            f"Approved news has no mappable coordinates: {record['source_url']}"
        )
    lat, lng, precision = coords
    return {
        "id": incident_id,
        "source_type": "news",
        "source_url": record["source_url"],
        "source_name": record.get("source_name"),
        "title": record.get("title"),
        "summary": record.get("summary"),
        "tambon": monitor_core.norm(place.get("tambon")),
        "amphoe": monitor_core.norm(place.get("amphoe")),
        "province": monitor_core.norm(place.get("province")),
        "lat": lat,
        "lng": lng,
        "precision": precision,
        "precision_th": monitor_core.PRECISION_TH.get(precision, ""),
        "substances": record.get("substances") or [],
        "tags": record.get("tags") or [],
        "event_date": record.get("event_date"),
    }


def publish_approved(
    queue_path=QUEUE_PATH,
    incidents_path=INCIDENTS_PATH,
    factories_path=FACTORIES_PATH,
):
    queue = _queue_data(queue_path)
    incidents = _read_json(incidents_path, [])
    known_urls = {
        record.get("source_url")
        for record in incidents
        if record.get("source_url")
    }
    approved = [
        record
        for record in queue["records"]
        if record["status"] == "approved" and not record.get("published", False)
    ]
    if not approved:
        print("No approved news to publish.")
        return 0

    factories = _read_json(factories_path)
    if not isinstance(factories, list) or not factories:
        raise ValueError(f"No factory coordinate records found in {factories_path}")

    next_id = max(
        (int(record.get("id", 0)) for record in incidents if str(record.get("id", "")).isdigit()),
        default=0,
    ) + 1
    new_incidents = []
    previous_db = monitor_core.DB
    with tempfile.TemporaryDirectory() as temp_dir:
        monitor_core.DB = str(Path(temp_dir) / "monitor.db")
        db = monitor_core.connect()
        try:
            monitor_core.import_factories(db, factories)
            for record in approved:
                if record["source_url"] in known_urls:
                    record["published"] = True
                    continue
                new_incidents.append(_public_incident(record, next_id, db))
                known_urls.add(record["source_url"])
                record["published"] = True
                next_id += 1
        finally:
            db.close()
            monitor_core.DB = previous_db

    if new_incidents:
        _write_json(incidents_path, new_incidents + incidents)
    _write_json(queue_path, queue)
    print(f"Published {len(new_incidents)} approved news items.")
    return len(new_incidents)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect")
    collect.add_argument("--results", default=str(RESULTS_PATH))
    collect.add_argument("--queue", default=str(QUEUE_PATH))
    collect.add_argument("--incidents", default=str(INCIDENTS_PATH))
    publish = subparsers.add_parser("publish")
    publish.add_argument("--queue", default=str(QUEUE_PATH))
    publish.add_argument("--incidents", default=str(INCIDENTS_PATH))
    publish.add_argument("--factories", default=str(FACTORIES_PATH))
    args = parser.parse_args()

    if args.command == "collect":
        collect_candidates(args.results, args.queue, args.incidents)
    else:
        publish_approved(args.queue, args.incidents, args.factories)


if __name__ == "__main__":
    main()
