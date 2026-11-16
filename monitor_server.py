#!/usr/bin/env python3
"""Local web server for the map and its incident review UI.

Run from the project directory with: python monitor_server.py
"""
import argparse
import json
import os
import re
import sqlite3
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import monitor_core

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
EXPORT_DIR = Path(os.environ.get("MONITOR_EXPORT_DIR", str(WEB_DIR / "data"))).expanduser().resolve()
configured_db = os.environ.get("MONITOR_DB")
monitor_core.DB = str(Path(configured_db).expanduser().resolve()) if configured_db else str(ROOT / "monitor.db")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def _send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlsplit(self.path).path == "/api/review":
            self._get_pending()
            return
        super().do_GET()

    def _get_pending(self):
        db = monitor_core.connect()
        try:
            rows = db.execute(
                "SELECT id,title,source_url,source_name,summary,tambon,amphoe,province,lat,lng,"
                "precision,substances,event_date,tags,created_at "
                "FROM incidents WHERE status='pending' ORDER BY id DESC"
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["substances"] = json.loads(item["substances"] or "[]")
                item["tags"] = json.loads(item["tags"] or "[]")
                item["precision_th"] = monitor_core.PRECISION_TH.get(item["precision"], "")
                items.append(item)
            self._send_json(200, {"items": items})
        except sqlite3.Error as exc:
            self.log_error("Could not load review queue: %s", exc)
            self._send_json(500, {"error": "อ่านคิวข่าวไม่สำเร็จ"})
        finally:
            db.close()

    def do_POST(self):
        match = re.fullmatch(r"/api/review/([0-9]+)/(approve|reject)", urlsplit(self.path).path)
        if not match:
            self._send_json(404, {"error": "ไม่พบรายการที่ร้องขอ"})
            return

        incident_id, action = int(match.group(1)), match.group(2)
        db = monitor_core.connect()
        try:
            if action == "approve":
                cur = db.execute(
                    "UPDATE incidents SET status='approved' "
                    "WHERE id=? AND status='pending' AND lat IS NOT NULL AND lng IS NOT NULL",
                    (incident_id,)
                )
            else:
                cur = db.execute(
                    "UPDATE incidents SET status='rejected' WHERE id=? AND status='pending'",
                    (incident_id,)
                )

            if cur.rowcount != 1:
                current = db.execute(
                    "SELECT status,lat,lng FROM incidents WHERE id=?", (incident_id,)
                ).fetchone()
                db.rollback()
                if current is None:
                    self._send_json(404, {"error": "ไม่พบข่าวนี้"})
                elif current["status"] != "pending":
                    self._send_json(409, {"error": "ข่าวนี้ไม่ได้อยู่ในคิวรอตรวจแล้ว"})
                else:
                    self._send_json(409, {"error": "ข่าวนี้ยังไม่มีพิกัด จึงอนุมัติขึ้นแผนที่ไม่ได้"})
                return

            db.commit()
            if action == "approve":
                try:
                    monitor_core.export_public(db, str(EXPORT_DIR))
                except (OSError, sqlite3.Error, ValueError) as exc:
                    self.log_error("Incident %s approved but map export failed: %s", incident_id, exc)
                    self._send_json(500, {"error": "อนุมัติข่าวแล้ว แต่ส่งออกข้อมูลแผนที่ไม่สำเร็จ กรุณาลองส่งออกข้อมูลอีกครั้ง"})
                    return
            self._send_json(200, {"id": incident_id, "status": "approved" if action == "approve" else "rejected"})
        except sqlite3.Error as exc:
            db.rollback()
            self.log_error("Could not update incident %s: %s", incident_id, exc)
            self._send_json(500, {"error": "บันทึกผลตรวจข่าวไม่สำเร็จ"})
        finally:
            db.close()


def main():
    parser = argparse.ArgumentParser(description="Serve the map and local news review UI")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"เปิดแผนที่และหน้าตรวจข่าวที่ http://127.0.0.1:{args.port}/")
    print(f"ฐานข้อมูล: {monitor_core.DB}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nปิดเซิร์ฟเวอร์แล้ว")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
