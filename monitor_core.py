#!/usr/bin/env python3
"""
monitor_core.py v2 - ฐานข้อมูล SQLite ของแผนที่เฝ้าระวัง: โรงงาน + จุดรายงานข่าว (ต้องผ่านการตรวจก่อนขึ้นแผนที่)

คำสั่ง:
  python monitor_core.py init
  python monitor_core.py import-factories --json factories_v3.json
  python monitor_core.py import-news --jsonl results_v33.jsonl
  python monitor_core.py review
  python monitor_core.py approve --id 3 5 8        (หรือ --all = อนุมัติทุกรายการที่มีพิกัด)
  python monitor_core.py reject --id 4
  python monitor_core.py set-coords --id 4 --lat 14.53 --lng 100.91
  python monitor_core.py add-incident --title "..." --url https://... --source manual --province สระบุรี --tambon พุแค
  python monitor_core.py export --out web/data
  python monitor_core.py stats

ถ้าตั้ง GOOGLE_MAPS_KEY (key ฝั่งเซิร์ฟเวอร์) และมี geocode_factories.py ในโฟลเดอร์เดียวกัน
ระบบจะหาพิกัดระดับตำบลจาก Google เมื่อตำบลนั้นไม่มีโรงงานในฐานข้อมูล
"""
import argparse, hashlib, json, os, sqlite3, statistics
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

DB = os.environ.get("MONITOR_DB", "monitor.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS factories(
  id TEXT PRIMARY KEY, reg_no TEXT, name TEXT, type_code TEXT, type_label TEXT, activity TEXT,
  year_be INTEGER, tambon TEXT, amphoe TEXT, province TEXT, lat REAL, lng REAL,
  precision TEXT, precision_th TEXT, row_hash TEXT, first_seen TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS incidents(
  id INTEGER PRIMARY KEY AUTOINCREMENT, source_type TEXT, source_url TEXT UNIQUE,
  title TEXT, summary TEXT, tambon TEXT, amphoe TEXT, province TEXT, lat REAL, lng REAL,
  precision TEXT, substances TEXT, event_date TEXT, source_name TEXT, text_source TEXT, tags TEXT,
  scope TEXT, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT);
CREATE TABLE IF NOT EXISTS source_state(key TEXT PRIMARY KEY, content_hash TEXT, checked_at TEXT);
CREATE INDEX IF NOT EXISTS idx_fac_area ON factories(province, amphoe, tambon);
"""

PUBLIC_FACTORY = ["id", "name", "type_code", "type_label", "activity", "year_be",
                  "tambon", "amphoe", "province", "lat", "lng", "precision", "precision_th"]
PUBLIC_INCIDENT = ["id", "source_type", "source_url", "source_name", "title", "summary", "tambon", "amphoe",
                   "province", "lat", "lng", "precision", "substances", "tags", "event_date"]
PRECISION_TH = {"point": "ตำแหน่งตามที่รายงาน",
                "tambon": "ระดับตำบล (ตำแหน่งโดยประมาณ)",
                "amphoe": "ระดับอำเภอ (ตำแหน่งโดยประมาณ ไม่ใช่จุดเกิดเหตุ)",
                "province": "ระดับจังหวัด (ตำแหน่งโดยประมาณ ไม่ใช่จุดเกิดเหตุ)"}
_PREFIX = ("ตำบล", "อำเภอ", "จังหวัด", "แขวง", "เขต", "ต.", "อ.", "จ.")


def norm(name):
    if not name:
        return None
    n = str(name).strip()
    for pre in _PREFIX:
        if n.startswith(pre):
            n = n[len(pre):].strip()
            break
    return n or None


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    cols = {r["name"] for r in db.execute("PRAGMA table_info(incidents)")}
    for col in ("source_name", "text_source", "tags", "scope"):
        if col not in cols:
            db.execute(f"ALTER TABLE incidents ADD COLUMN {col} TEXT")
    db.commit()
    return db


def file_changed(db, key, data: bytes) -> bool:
    h = hashlib.sha256(data).hexdigest()
    row = db.execute("SELECT content_hash FROM source_state WHERE key=?", (key,)).fetchone()
    db.execute("INSERT INTO source_state(key,content_hash,checked_at) VALUES(?,?,?) "
               "ON CONFLICT(key) DO UPDATE SET content_hash=excluded.content_hash, checked_at=excluded.checked_at",
               (key, h, now()))
    db.commit()
    return row is None or row["content_hash"] != h


def _row_hash(r):
    keys = ["name", "type_code", "activity", "tambon", "amphoe", "province", "lat", "lng", "precision"]
    return hashlib.sha1("|".join(str(r.get(k)) for k in keys).encode("utf-8")).hexdigest()


def import_factories(db, records):
    new = changed = same = 0
    for r in records:
        fid = str(r.get("id") or r.get("reg_no") or "").strip()
        if not fid or r.get("lat") is None or r.get("lng") is None:
            continue
        h = _row_hash(r)
        old = db.execute("SELECT row_hash FROM factories WHERE id=?", (fid,)).fetchone()
        vals = (fid, r.get("reg_no"), r.get("name"), r.get("type_code"), r.get("type_label"),
                r.get("activity"), int(r["year_be"]) if str(r.get("year_be", "")).isdigit() else None,
                r.get("tambon"), r.get("amphoe"), r.get("province"), r["lat"], r["lng"],
                r.get("precision"), r.get("precision_th"), h)
        if old is None:
            db.execute("INSERT INTO factories(id,reg_no,name,type_code,type_label,activity,year_be,tambon,amphoe,"
                       "province,lat,lng,precision,precision_th,row_hash,first_seen,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals + (now(), now()))
            new += 1
        elif old["row_hash"] != h:
            db.execute("UPDATE factories SET reg_no=?,name=?,type_code=?,type_label=?,activity=?,year_be=?,tambon=?,"
                       "amphoe=?,province=?,lat=?,lng=?,precision=?,precision_th=?,row_hash=?,updated_at=? WHERE id=?",
                       vals[1:] + (now(), fid))
            changed += 1
        else:
            same += 1
    db.commit()
    return new, changed, same


def _median_coords(rows):
    if not rows:
        return None
    return statistics.median(r["lat"] for r in rows), statistics.median(r["lng"] for r in rows)


def google_tambon(province, amphoe, tambon):
    """หาพิกัดระดับตำบลจาก Google (ถ้ามี key) ตรวจจังหวัดและชื่อตำบลก่อนใช้"""
    if not os.environ.get("GOOGLE_MAPS_KEY"):
        return None
    try:
        import geocode_factories as gf
    except Exception:
        return None
    q = " ".join(filter(None, [f"ตำบล{tambon}", f"อำเภอ{amphoe}" if amphoe else "", f"จังหวัด{province}"]))
    cache = json.load(open(gf.CACHE_FILE, encoding="utf-8")) if os.path.exists(gf.CACHE_FILE) else {}
    try:
        g = gf.geocode(q, cache)
    except Exception as e:
        print(f"    (Google Geocoding ใช้ไม่ได้: {e})")
        return None
    finally:
        gf.save_cache(cache)
    if g.get("status") != "OK":
        return None
    in_th, pm = gf.evaluate(g, province)
    if in_th and pm and tambon in (g.get("formatted") or ""):
        return g["lat"], g["lng"], "tambon"
    return None


def place_to_coords(db, province=None, amphoe=None, tambon=None):
    """ไล่ระดับ: ตำบล (จากโรงงาน) -> ตำบล (Google) -> อำเภอ -> จังหวัด"""
    province, amphoe, tambon = norm(province), norm(amphoe), norm(tambon)
    if amphoe == "เมือง" and province:
        amphoe = "เมือง" + province
    if province and amphoe and tambon:
        c = _median_coords(db.execute("SELECT lat,lng FROM factories WHERE province=? AND amphoe=? AND tambon=?",
                                      (province, amphoe, tambon)).fetchall())
        if c:
            return c[0], c[1], "tambon"
    if province and tambon:
        g = google_tambon(province, amphoe, tambon)
        if g:
            return g
    if province and amphoe:
        c = _median_coords(db.execute("SELECT lat,lng FROM factories WHERE province=? AND amphoe=?",
                                      (province, amphoe)).fetchall())
        if c:
            return c[0], c[1], "amphoe"
    if province:
        c = _median_coords(db.execute("SELECT lat,lng FROM factories WHERE province=?", (province,)).fetchall())
        if c:
            return c[0], c[1], "province"
    return None


def add_incident(db, source_type, source_url, title, summary=None, province=None, amphoe=None,
                 tambon=None, substances=None, event_date=None, lat=None, lng=None,
                 source_name=None, text_source=None, tags=None, scope=None):
    prec = "point"
    if lat is None or lng is None:
        res = place_to_coords(db, province, amphoe, tambon)
        lat, lng, prec = res if res else (None, None, None)
    amp = norm(amphoe)
    if amp == "เมือง" and norm(province):
        amp = "เมือง" + norm(province)
    cur = db.execute(
        "INSERT OR IGNORE INTO incidents(source_type,source_url,title,summary,tambon,amphoe,province,lat,lng,"
        "precision,substances,event_date,source_name,text_source,tags,scope,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
        (source_type, source_url, title, summary, norm(tambon), amp, norm(province), lat, lng, prec,
         json.dumps(substances or [], ensure_ascii=False), event_date, source_name, text_source,
         json.dumps(sorted(set(tags or [])), ensure_ascii=False), scope, now()))
    db.commit()
    return cur.lastrowid if cur.rowcount else None


def import_news(db, jsonl_path):
    added = dup = skipped = 0
    with open(jsonl_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            news = d.get("news") or {}
            url = news.get("resolved_url") or news.get("link")
            place = d.get("place") or {}
            if (not url or not d.get("relevant") or d.get("scope") == "national"
                    or not any(place.get(k) for k in ("tambon", "amphoe", "province"))):
                skipped += 1
                continue
            ev = d.get("event_date")                       # วันเกิดเหตุตามข่าวก่อน
            if not ev:
                try:
                    ev = parsedate_to_datetime(news["published"]).date().isoformat()
                except Exception:
                    ev = None
            i = add_incident(db, "news", url, news.get("title", "")[:200], d.get("summary_th"),
                             place.get("province"), place.get("amphoe"), place.get("tambon"),
                             d.get("substances") or [], ev, source_name=news.get("source"),
                             text_source=d.get("text_source"), tags=d.get("tags") or [], scope=d.get("scope"))
            if i:
                added += 1
            else:
                dup += 1
    return added, dup, skipped


def export_public(db, outdir):
    os.makedirs(outdir, exist_ok=True)
    fac = [{k: r[k] for k in PUBLIC_FACTORY} for r in db.execute("SELECT * FROM factories ORDER BY id")]
    inc = []
    for r in db.execute("SELECT * FROM incidents WHERE status='approved' AND lat IS NOT NULL ORDER BY id DESC"):
        d = {k: r[k] for k in PUBLIC_INCIDENT}
        d["substances"] = json.loads(d["substances"] or "[]")
        d["tags"] = json.loads(d["tags"] or "[]")
        d["precision_th"] = PRECISION_TH.get(d["precision"], "")
        inc.append(d)
    for name, data in (("factories.json", fac), ("incidents.json", inc)):
        tmp = os.path.join(outdir, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, os.path.join(outdir, name))
    return len(fac), len(inc)


def show_pending(db):
    rows = db.execute("SELECT * FROM incidents WHERE status='pending' ORDER BY id").fetchall()
    for r in rows:
        where = " ".join(filter(None, [r["tambon"], r["amphoe"], r["province"]]))
        subs = ", ".join(json.loads(r["substances"] or "[]")) or "ไม่ระบุชนิดสาร"
        tags = ", ".join(json.loads(r["tags"] or "[]"))
        prec = r["precision"] or "ยังไม่มีพิกัด -> ใช้ set-coords ก่อน approve"
        print(f"[{r['id']}] {r['title']}")
        print(f"    {where} | ขอบเขต: {r['scope'] or '-'} | พิกัด: {prec} | วันที่: {r['event_date'] or '-'}")
        print(f"    สาร: {subs}" + (f" | ป้าย: {tags}" if tags else ""))
        if r["summary"]:
            print(f"    {r['summary']}")
        print(f"    {r['source_url']}\n")
    print(f"รอตรวจ {len(rows)} รายการ")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init"); sub.add_parser("review"); sub.add_parser("stats")
    p = sub.add_parser("import-factories"); p.add_argument("--json", required=True)
    p = sub.add_parser("add-incident")
    for x in ("title", "url", "source"):
        p.add_argument("--" + x, required=True)
    for x in ("summary", "province", "amphoe", "tambon", "substances", "tags", "date"):
        p.add_argument("--" + x)
    p.add_argument("--lat", type=float); p.add_argument("--lng", type=float)
    p = sub.add_parser("import-news"); p.add_argument("--jsonl", required=True)
    p = sub.add_parser("approve"); p.add_argument("--id", type=int, nargs="+"); p.add_argument("--all", action="store_true")
    p = sub.add_parser("reject"); p.add_argument("--id", type=int, nargs="+", required=True)
    p = sub.add_parser("set-coords")
    p.add_argument("--id", type=int, required=True); p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lng", type=float, required=True)
    p = sub.add_parser("export"); p.add_argument("--out", default="web/data")
    a = ap.parse_args()
    db = connect()

    if a.cmd == "init":
        print("พร้อมแล้ว:", os.path.abspath(DB))
    elif a.cmd == "import-factories":
        data = open(a.json, "rb").read()
        ch = file_changed(db, "factories:" + os.path.basename(a.json), data)
        n, c, s = import_factories(db, json.loads(data))
        print(f"ไฟล์ต้นทาง{'เปลี่ยนจากครั้งก่อน' if ch else 'ไม่เปลี่ยน'} | ใหม่ {n} | แก้ไข {c} | เหมือนเดิม {s}")
    elif a.cmd == "add-incident":
        subs = [x.strip() for x in (a.substances or "").split(",") if x.strip()]
        tg = [x.strip() for x in (a.tags or "").split(",") if x.strip()]
        i = add_incident(db, a.source, a.url, a.title, a.summary, a.province, a.amphoe, a.tambon,
                         subs, a.date, a.lat, a.lng, tags=tg, scope="site")
        print(f"เพิ่มแล้ว id = {i} (pending)" if i else "มีลิงก์นี้อยู่แล้ว ไม่เพิ่มซ้ำ")
    elif a.cmd == "review":
        show_pending(db)
    elif a.cmd == "import-news":
        n, d, k = import_news(db, a.jsonl)
        print(f"นำเข้าข่าว: เพิ่ม {n} (pending) | ซ้ำลิงก์ {d} | ข้าม {k}")
    elif a.cmd == "approve":
        if a.all:
            cur = db.execute("UPDATE incidents SET status='approved' WHERE status='pending' AND lat IS NOT NULL")
        elif a.id:
            cur = db.execute(f"UPDATE incidents SET status='approved' WHERE id IN ({','.join('?' * len(a.id))})", a.id)
        else:
            raise SystemExit("ใส่ --id หรือ --all")
        db.commit()
        print(f"อนุมัติแล้ว {cur.rowcount} รายการ (รัน export เพื่ออัปเดตหน้าเว็บ)")
    elif a.cmd == "reject":
        cur = db.execute(f"UPDATE incidents SET status='rejected' WHERE id IN ({','.join('?' * len(a.id))})", a.id)
        db.commit()
        print(f"ปฏิเสธแล้ว {cur.rowcount} รายการ")
    elif a.cmd == "set-coords":
        cur = db.execute("UPDATE incidents SET lat=?, lng=?, precision='point' WHERE id=?", (a.lat, a.lng, a.id))
        db.commit()
        print("ใส่พิกัดแล้ว" if cur.rowcount else "ไม่พบ id นี้")
    elif a.cmd == "export":
        nf, ni = export_public(db, a.out)
        print(f"ส่งออกแล้ว: โรงงาน {nf} | จุดเหตุที่อนุมัติ {ni} -> {a.out}")
    elif a.cmd == "stats":
        for t in ("factories", "incidents"):
            print(t, db.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"])
        for r in db.execute("SELECT status, COUNT(*) c FROM incidents GROUP BY status"):
            print("  incidents", r["status"], r["c"])


if __name__ == "__main__":
    main()