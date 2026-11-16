#!/usr/bin/env python3
"""
toxic_news_v33.py - ดึงข่าว -> Gemini สกัดข้อมูล -> บันทึก results_v33.jsonl (ไม่ตัดข่าวที่มีมูลทิ้ง แต่จัดตามขอบเขตพื้นที่)

ขอบเขต (scope):  site = จุดเฉพาะ | area = ระดับอำเภอ/จังหวัด | national = ระดับประเทศ/หลายจังหวัด (ไม่ปักบนแผนที่ แสดงในรายการข่าว)
ข่าวถูกข้ามเฉพาะที่เป็นความเห็น/การเมืองที่ไม่มีข้อเท็จจริงใหม่ หรือไม่เกี่ยวกับของเสียโรงงาน

ติดตั้ง:   pip install google-genai requests trafilatura googlenewsdecoder
ตั้งค่า key (อย่าเขียน key ลงในไฟล์):
  Windows (cmd):  set GEMINI_API_KEY=ใส่key
ตัวเลือก: GEMINI_MODEL, MAX_ITEMS (ค่าเริ่มต้น 20), ENABLE_FULLTEXT (1/0), GEMINI_INTERVAL (วินาที ค่าเริ่มต้น 10)
รัน:   python toxic_news_v33.py
"""
import os, re, sys, json, html, time, traceback, itertools
import xml.etree.ElementTree as ET
from urllib.parse import quote
import requests
from google import genai
from pathlib import Path
from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().with_name(".env"))

VERSION = "v3.3"
KEY = os.environ.get("GEMINI_API_KEY")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "20"))
ENABLE_FULLTEXT = os.environ.get("ENABLE_FULLTEXT", "1") == "1"
MIN_INTERVAL = float(os.environ.get("GEMINI_INTERVAL", "10"))
OUT_FILE = "results_v33.jsonl"
FULLTEXT_CHARS = 6000
_last_call = [0.0]

QUERIES = [
    "กากอุตสาหกรรม ลักลอบทิ้ง when:7d",
    "ขยะพิษ when:7d",
    "แบตเตอรี่ลิเธียม โรงงาน ตรวจพบ when:7d",
    "สารเคมี รั่วไหล โรงงาน when:7d",
    "โรงงานรีไซเคิล ขยะอิเล็กทรอนิกส์ when:7d",
    "โรงงานเถื่อน ไม่มีใบอนุญาต when:7d",
    "ลักลอบประกอบกิจการโรงงาน กากอุตสาหกรรม when:7d",
    "สั่งหยุดกิจการ โรงงานจัดการกากอุตสาหกรรม when:7d",
    "ระงับการประกอบกิจการ โรงงานรีไซเคิล when:7d",
    "ผลตรวจโรงงาน 105 106 พบฝ่าฝืน when:7d",
]
ALLOWED_TAGS = {"unlicensed", "stop_order", "illegal_dumping", "inspection_violations", "disputed"}
ALLOWED_SCOPE = {"site", "area", "national"}

PROMPT = """คุณเป็นผู้ช่วยสกัดข้อมูลจากข่าวภาษาไทยเรื่องขยะพิษ/กากอุตสาหกรรม/สารเคมีอันตราย เพื่อทำการเตือนประชาชน
ใช้เฉพาะข้อมูลในข้อความด้านล่างเท่านั้น ห้ามเดา ห้ามเติมข้อมูลที่ไม่มี (ไม่มีให้ใส่ null หรือ [])
ตอบเป็น JSON object เดียว รูปแบบ:
{
 "relevant": true/false,
 "scope": "site"|"area"|"national"|null,
 "place": {"village": str|null, "moo": str|null, "tambon": str|null, "amphoe": str|null, "province": str|null},
 "location_precision": "village"|"tambon"|"amphoe"|"province"|"unknown",
 "event_date": "YYYY-MM-DD"|null,
 "substances": [str],
 "tags": [str],
 "event_type": "found"|"risk"|"other",
 "status": "reported"|"confirmed_by_authority",
 "summary_th": str
}
ความหมาย:
- relevant = true เมื่อข่าวรายงานข้อเท็จจริงเกี่ยวกับกากอุตสาหกรรม/ของเสียอันตราย/มลพิษจากโรงงานในประเทศไทย เช่น การตรวจพบ ผลตรวจของหน่วยงานรัฐ
  คำสั่งของหน่วยงานรัฐ หรือการชี้แจงว่าไม่พบเหตุตามที่เป็นข่าว  relevant = false เฉพาะข่าวความเห็น/การเมือง/การอภิปรายที่ไม่มีข้อเท็จจริงใหม่ ข่าวต่างประเทศ หรือไม่เกี่ยวกับของเสียโรงงาน
- scope: "site" = ระบุสถานที่เฉพาะ (หมู่บ้าน/ตำบล/ที่ตั้งโรงงานหรือกองของเสีย), "area" = ระบุเป็นระดับอำเภอหรือจังหวัด (เช่น ผลตรวจโรงงานทั้งจังหวัด),
  "national" = ระดับประเทศหรือหลายจังหวัดโดยไม่ได้เจาะจงที่เดียว (place ให้ว่างทั้งหมด)
- event_date = วันที่เกิดเหตุ/ตรวจพบ/สั่งการที่ข่าวระบุจริง รูปแบบ YYYY-MM-DD เป็นปี ค.ศ. (ถ้าข่าวใช้ พ.ศ. ให้ลบ 543) ถ้าข่าวไม่ระบุวันที่ ให้ใส่ null อย่าใช้วันที่เผยแพร่ข่าว
- substances = ชนิดสารที่ข่าวระบุจริงเท่านั้น (เช่น แคดเมียม, แบตเตอรี่ลิเธียม, ตะกั่ว) ถ้าข่าวบอกแค่ "กากอุตสาหกรรม" ให้ใส่ []
- tags = ป้ายที่ข่าวระบุจริงเท่านั้น เลือกได้จาก:
  "unlicensed" (ข่าวอ้างว่าหน่วยงานรัฐตรวจพบหรือแถลงว่าสถานที่นี้ประกอบกิจการโรงงานโดยไม่มีใบอนุญาต),
  "stop_order" (ข่าวระบุว่าหน่วยงานรัฐมีคำสั่งหยุดหรือระงับกิจการ),
  "illegal_dumping" (ข่าวระบุว่าเป็นการลักลอบทิ้ง),
  "inspection_violations" (ข่าวระบุผลตรวจของหน่วยงานรัฐที่พบการฝ่าฝืน),
  "disputed" (ข่าวระบุว่าข้อกล่าวอ้างหรือภาพก่อนหน้านี้ไม่เป็นความจริง หรือไม่พบเหตุตามที่เป็นข่าว)
  ห้ามใส่ unlicensed หรือ stop_order ถ้าเป็นเพียงการสงสัย ร้องเรียน หรือกล่าวอ้างโดยไม่มีหน่วยงานรัฐยืนยัน ไม่เข้าเกณฑ์ให้ใส่ []
- summary_th สรุปไม่เกิน 2 ประโยค ใช้คำว่า "พบ/รายงานว่า" ห้ามกล่าวหาบุคคลหรือบริษัท ห้ามใส่ชื่อบริษัท
  ถ้าข่าวบอกว่ายังไม่มีผลตรวจยืนยัน ให้ใส่ข้อความนั้นในสรุปด้วย

ข่าว:
หัวข้อ: {title}
สรุป: {summary}
แหล่งข่าว: {source}
{fulltext_block}"""


def strip_tags(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def fetch_news():
    per_query, seen = [], set()
    for q in QUERIES:
        url = f"https://news.google.com/rss/search?q={quote(q)}&hl=th&gl=TH&ceid=TH:th"
        got = []
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:
            print(f"[ข้าม] ดึง RSS ไม่ได้ ({q}): {e}")
            per_query.append(got)
            continue
        for it in root.iter("item"):
            title = strip_tags(it.findtext("title"))
            key = re.sub(r"\W+", "", title)[:60]
            if not title or key in seen:
                continue
            seen.add(key)
            got.append({"title": title, "link": it.findtext("link"), "published": it.findtext("pubDate"),
                        "summary": strip_tags(it.findtext("description")), "source": strip_tags(it.findtext("source")),
                        "query": q})
        per_query.append(got)
    # สลับหยิบทีละคำค้น เพื่อให้ทุกคำค้นได้ถูกตรวจ แม้ตั้ง MAX_ITEMS ไว้น้อย
    return [x for grp in itertools.zip_longest(*per_query) for x in grp if x]


def build_prompt(item, fulltext=None):
    block = ("เนื้อข่าวฉบับเต็ม (ตัดทอน):\n" + fulltext[:FULLTEXT_CHARS]) if fulltext else ""
    return (PROMPT.replace("{title}", item["title"]).replace("{summary}", item["summary"])
                  .replace("{source}", item["source"]).replace("{fulltext_block}", block))


def ask_gemini(client, prompt):
    last_err = None
    for attempt in range(5):
        wait = MIN_INTERVAL - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        try:
            resp = client.models.generate_content(
                model=MODEL, contents=prompt,
                config={"response_mime_type": "application/json", "temperature": 0.1,
                        "automatic_function_calling": {"disable": True}})
            txt = re.sub(r"^```(?:json)?|```$", "", (resp.text or "").strip(), flags=re.M).strip()
            return json.loads(txt)
        except Exception as e:
            last_err = e
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                if "PerDay" in msg or "per day" in msg.lower():
                    sys.exit("โควตารายวันของ Gemini key หมด (หรือโมเดลนี้ไม่มีโควตาฟรี)\n"
                             "ทางเลือก: รอรีเซ็ตวันถัดไป, เปลี่ยนโมเดลด้วย GEMINI_MODEL, หรือเปิดการเรียกเก็บเงินใน Google AI Studio\n"
                             f"รายละเอียด: {msg[:400]}")
                m = re.search(r"retry in ([\d.]+)s", msg) or re.search(r"retryDelay['\"]?:\s*['\"]?(\d+)", msg)
                delay = float(m.group(1)) + 2 if m else 15 * (attempt + 1)
                print(f"    (โดนจำกัดอัตราเรียก รอ {delay:.0f} วินาที แล้วลองใหม่ ครั้งที่ {attempt + 1}/5)")
                time.sleep(delay)
                continue
            if attempt >= 2:
                raise
            time.sleep(2)
    raise RuntimeError(f"ลองครบแล้วยังไม่สำเร็จ: {str(last_err)[:300]}")


def get_fulltext(link):
    """ถอดลิงก์ Google News -> ดึงหน้าเว็บ -> สกัดข้อความ  คืน (ข้อความ|None, ลิงก์จริง|เหตุผล)"""
    try:
        from googlenewsdecoder import gnewsdecoder
        import trafilatura
    except ImportError:
        return None, "ไม่ได้ติดตั้ง googlenewsdecoder/trafilatura"
    try:
        res = None
        for _ in range(2):
            res = gnewsdecoder(link, interval=2)
            if res and res.get("status"):
                break
            time.sleep(3)
        if not (res and res.get("status")):
            return None, f"ถอดลิงก์ไม่ได้ (ผลที่ได้: {repr(res)[:200]})"
        real = res["decoded_url"]
        r = requests.get(real, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        text = trafilatura.extract(r.text)
        if not text or len(text) < 200:
            return None, "ดึงเนื้อข่าวไม่ได้/สั้นเกินไป"
        return text, real
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def normalize(d):
    if not isinstance(d, dict):
        return None
    if not isinstance(d.get("substances"), list):
        d["substances"] = []
    if not isinstance(d.get("place"), dict):
        d["place"] = {}
    tags = d.get("tags")
    d["tags"] = sorted({t for t in tags if t in ALLOWED_TAGS}) if isinstance(tags, list) else []
    ed = d.get("event_date")
    d["event_date"] = ed if isinstance(ed, str) and re.fullmatch(r"20[2-3]\d-\d{2}-\d{2}", ed) else None
    p = d["place"]
    if d.get("scope") not in ALLOWED_SCOPE:                      # สรุปขอบเขตเองถ้าโมเดลไม่ได้ให้มา/ให้ผิด
        d["scope"] = ("site" if (p.get("village") or p.get("tambon")) else
                      "area" if (p.get("amphoe") or p.get("province")) else "national")
    if d["scope"] == "national":
        d["place"] = {}
    return d


def has_place(d):
    return any((d.get("place") or {}).get(k) for k in ("village", "tambon", "amphoe", "province"))


def needs_more(d):
    """ดึงเนื้อข่าวเต็มเฉพาะข่าวจุดเฉพาะที่ยังขาดชนิดสารหรือพิกัดหยาบ (ประหยัดโควตา)"""
    return d.get("scope") == "site" and (not d["substances"] or d.get("location_precision") in ("province", "unknown", None))


SCOPE_TH = {"site": "จุดเฉพาะ", "area": "ระดับพื้นที่", "national": "ระดับประเทศ"}


def main():
    if not KEY:
        sys.exit("ยังไม่ได้ตั้ง GEMINI_API_KEY (ดูวิธีตั้งค่าที่หัวไฟล์)")
    print(f"สคริปต์ {VERSION} | ไฟล์: {os.path.abspath(__file__)}")
    client = genai.Client(api_key=KEY)
    news = fetch_news()
    n = min(MAX_ITEMS, len(news))
    print(f"ดึงข่าวได้ {len(news)} ชิ้น (ไม่ซ้ำ) จะทดสอบ {n} ชิ้น | โมเดล: {MODEL} | ดึงเนื้อข่าวเต็ม: {'เปิด' if ENABLE_FULLTEXT else 'ปิด'}\n")

    cnt = {"site": 0, "area": 0, "national": 0, "skipped": 0, "full": 0}
    shown_tb = False
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        for item in news[:n]:
            try:
                d = normalize(ask_gemini(client, build_prompt(item)))
                src = "headline"
                if d and d.get("relevant") and ENABLE_FULLTEXT and needs_more(d):
                    text, info = get_fulltext(item["link"])
                    if text:
                        d2 = normalize(ask_gemini(client, build_prompt(item, text)))
                        if d2 and d2.get("relevant"):
                            d, src = d2, "fulltext"
                            cnt["full"] += 1
                            item["resolved_url"] = info
                    else:
                        print(f"    (ดึงเนื้อข่าวเต็มไม่ได้: {info})")
            except Exception as e:
                print(f"[พลาด] {item['title'][:50]}... -> {e}")
                if not shown_tb:
                    traceback.print_exc()
                    shown_tb = True
                continue

            if not d or not d.get("relevant"):
                cnt["skipped"] += 1
                print(f"[ข้าม:ไม่เกี่ยวข้อง/ไม่มีข้อเท็จจริงใหม่] {item['title'][:60]}")
                continue
            if d["scope"] != "national" and not has_place(d):
                cnt["skipped"] += 1
                print(f"[ข้าม:ไม่มีสถานที่] {item['title'][:60]}")
                continue

            cnt[d["scope"]] += 1
            p = d["place"]
            where = " ".join(filter(None, [p.get("village"), p.get("tambon"), p.get("amphoe"), p.get("province")])) or "ทั่วประเทศ/หลายจังหวัด"
            print(f"[{SCOPE_TH[d['scope']]}] {where} | พิกัด: {d.get('location_precision')} | วันที่เหตุ: {d.get('event_date') or 'ไม่ระบุ'} | อ่านจาก: {src}")
            print(f"    สาร: {', '.join(d['substances']) or 'ไม่ระบุชนิดสาร'} | {d.get('event_type')} | {d.get('status')}"
                  + (f" | ป้าย: {', '.join(d['tags'])}" if d["tags"] else ""))
            print(f"    {d.get('summary_th')}\n    ข่าว: {item['title'][:70]}\n    {item['link']}\n")
            f.write(json.dumps({**d, "text_source": src, "news": item}, ensure_ascii=False) + "\n")
            f.flush()

    print(f"สรุป: จุดเฉพาะ {cnt['site']} | ระดับพื้นที่ {cnt['area']} | ระดับประเทศ {cnt['national']} | ข้าม {cnt['skipped']} | อ่านเนื้อข่าวเต็มสำเร็จ {cnt['full']} -> {OUT_FILE}")


if __name__ == "__main__":
    main()
