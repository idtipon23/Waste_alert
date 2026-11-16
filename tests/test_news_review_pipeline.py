import json
import tempfile
import unittest
from pathlib import Path

from news_review_pipeline import collect_candidates, publish_approved


class NewsReviewPipelineTests(unittest.TestCase):
    def test_collect_deduplicates_against_queue_and_public_incidents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            results = root / "results.jsonl"
            queue = root / "queue.json"
            incidents = root / "incidents.json"
            results.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in (
                        self._candidate("https://news.test/a"),
                        self._candidate("https://news.test/b"),
                        self._candidate("https://news.test/b"),
                    )
                ),
                encoding="utf-8",
            )
            queue.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "records": [
                            {
                                "status": "rejected",
                                "source_url": "https://news.test/a",
                                "place": {},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            incidents.write_text("[]", encoding="utf-8")

            added = collect_candidates(str(results), str(queue), str(incidents))

            self.assertEqual(added, 1)
            stored = json.loads(queue.read_text(encoding="utf-8"))
            self.assertEqual(stored["records"][-1]["source_url"], "https://news.test/b")
            self.assertEqual(stored["records"][-1]["status"], "pending")

    def test_publish_adds_only_approved_records_and_marks_them_published(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            queue = root / "queue.json"
            incidents = root / "incidents.json"
            factories = root / "factories.json"
            queue.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "records": [
                            self._review_record("https://news.test/approved", "approved"),
                            self._review_record("https://news.test/pending", "pending"),
                        ],
                    }
                ),
                encoding="utf-8",
            )
            incidents.write_text("[]", encoding="utf-8")
            factories.write_text(
                json.dumps(
                    [
                        {
                            "id": "factory-1",
                            "name": "Factory",
                            "type_code": "10500",
                            "activity": "recycling",
                            "tambon": "ทดสอบ",
                            "amphoe": "ทดสอบ",
                            "province": "ทดสอบ",
                            "lat": 13.5,
                            "lng": 100.5,
                            "precision": "point",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            published = publish_approved(str(queue), str(incidents), str(factories))

            self.assertEqual(published, 1)
            map_records = json.loads(incidents.read_text(encoding="utf-8"))
            self.assertEqual(len(map_records), 1)
            self.assertEqual(map_records[0]["source_url"], "https://news.test/approved")
            queue_records = json.loads(queue.read_text(encoding="utf-8"))["records"]
            self.assertTrue(queue_records[0]["published"])
            self.assertFalse(queue_records[1].get("published", False))

    @staticmethod
    def _candidate(url):
        return {
            "relevant": True,
            "scope": "site",
            "place": {"province": "จังหวัดทดสอบ"},
            "summary_th": "รายงานเหตุการณ์",
            "news": {"link": url, "title": "ข่าวทดสอบ", "source": "สำนักข่าว"},
        }

    @staticmethod
    def _review_record(url, status):
        return {
            "status": status,
            "published": False,
            "source_url": url,
            "source_name": "สำนักข่าว",
            "title": "ข่าวทดสอบ",
            "summary": "รายงานเหตุการณ์",
            "place": {
                "tambon": "ตำบลทดสอบ",
                "amphoe": "อำเภอทดสอบ",
                "province": "จังหวัดทดสอบ",
            },
            "substances": [],
            "tags": [],
            "event_date": "2026-11-16",
        }


if __name__ == "__main__":
    unittest.main()
