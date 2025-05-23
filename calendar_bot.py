# calendar_bot.py (fix escape of '.' in description numbering + MarkdownV2)
import requests, json, asyncio, logging, textwrap, re, yaml
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from telegram import Bot
from lxml import etree
from icalendar import Calendar
import pytz
import os

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger("calendar_bot")

#with open("config.yaml", "r") as f:
#    cfg = yaml.safe_load(f)
#TG_TOKEN   = cfg["telegram"]["token"]
#TG_CHAT_ID = cfg["telegram"]["chat_id"]
#CAL_URL  = cfg["caldav"]["url"]
#CAL_USER = cfg["caldav"]["username"]
#CAL_PASS = cfg["caldav"]["password"]
#STORE_FILE = Path("last_events.json")

TG_TOKEN   = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
CAL_URL    = os.environ["CAL_URL"]
CAL_USER   = os.environ["CAL_USER"]
CAL_PASS   = os.environ["CAL_PASS"]
STORE_FILE = Path(".cache/last_events.json")

ESCAPE_CHARS = r"_ * [ ] ( ) ~ ` > # + - = | { } . !".split()

def escape_md(text):
    def esc(match):
        char = match.group(0)
        return f"\\{char}"
    return re.sub(r"([" + re.escape(''.join(ESCAPE_CHARS)) + r"])", esc, text)

def bold_md(text):
    return "*" + escape_md(text) + "*"

def fmt_time_range(ev):
    start = datetime.fromisoformat(ev["start"])
    end = datetime.fromisoformat(ev["end"])
    wd = ['Thứ 2','Thứ 3','Thứ 4','Thứ 5','Thứ 6','Thứ 7','Chủ nhật'][start.weekday()]
    return f"{wd}, {start.strftime('%Y-%m-%d %H:%M')}-{end.strftime('%H:%M')}"

def parse_description(text):
    lines = text.strip().splitlines()
    result, current_item, current_number = [], "", ""
    for line in lines:
        line = line.rstrip()
        match = re.match(r"^\s*(\d{1,2})\.\s+(.*)", line)
        if match:
            if current_number:
                line_text = current_item.strip()
                result.append(escape_md(f"{current_number}. {line_text}"))
            current_number = match.group(1)
            current_item = match.group(2)
        else:
            current_item += " " + line.strip()
    if current_number:
        line_text = current_item.strip()
        result.append(escape_md(f"{current_number}. {line_text}"))
    return result

def get_chu_tri(desc):
    match = re.search(r"4\.\s*Chủ trì: ([^\n]*)", desc)
    return match.group(1).strip() if match else ""

def fetch_events(days=7):
    try:
        now = datetime.now(timezone.utc)
        start = now.strftime("%Y%m%dT%H%M%SZ")
        end = (now + timedelta(days=days)).strftime("%Y%m%dT%H%M%SZ")
        log.info(f"📡 Truy vấn CalDAV từ {start} → {end}")

        report_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<cal:calendar-query xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <d:getetag/>
    <cal:calendar-data/>
  </d:prop>
  <cal:filter>
    <cal:comp-filter name="VCALENDAR">
      <cal:comp-filter name="VEVENT">
        <cal:time-range start="{start}" end="{end}"/>
      </cal:comp-filter>
    </cal:comp-filter>
  </cal:filter>
</cal:calendar-query>"""

        headers = {
            "Depth": "1",
            "Content-Type": "application/xml"
        }

        res = requests.request(
            "REPORT", CAL_URL,
            auth=(CAL_USER, CAL_PASS),
            headers=headers,
            data=report_xml
        )

        if res.status_code not in (200, 207):
            log.error(f"❌ CalDAV error {res.status_code}")
            return None  # ❌ Dừng xử lý tiếp
            
        tree = etree.parse(BytesIO(res.content))
        ns = {"cal": "urn:ietf:params:xml:ns:caldav"}
        events = []
        for b in tree.findall(".//cal:calendar-data", namespaces=ns):
            cal = Calendar.from_ical(b.text.encode())
            for comp in cal.walk():
                if comp.name != "VEVENT": continue
                events.append({
                    "uid": str(comp.get("uid")),
                    "start": comp.get("dtstart").dt.isoformat(),
                    "end": comp.get("dtend").dt.isoformat(),
                    "summary": str(comp.get("summary", "")),
                    "location": str(comp.get("location", "")),
                    "desc_raw": str(comp.get("description", ""))
                })
        return sorted(events, key=lambda x: x["start"])

    except Exception as e:
        log.exception("❌ Lỗi khi truy cập CalDAV:")
        return None

def load_previous():
    return json.loads(STORE_FILE.read_text()) if STORE_FILE.exists() else []

def normalize_event(e):
    """Chuẩn hóa sự kiện để so sánh"""
    return {
        "uid": e["uid"],
        "summary": e.get("summary", "").strip(),
        "location": e.get("location", "").strip(),
        "description": e.get("description", "").strip(),
        "start": str(e["start"]),
        "end": str(e["end"]),
        "chu_tri": e.get("chu_tri", "").strip(),
    }

def diff_events(prev, cur):
    def to_map(events):
        return {e["uid"]: normalize_event(e) for e in events}

    prev_map = to_map(prev)
    cur_map = to_map(cur)

    added, changed, removed = [], [], []


    for uid in cur_map:
        if uid not in prev_map:
            log.debug(f"[diff_events] ADD {uid}")
            added.append(cur_map[uid])
        elif json.dumps(cur_map[uid], sort_keys=True) != json.dumps(prev_map[uid], sort_keys=True):
            log.debug(f"[diff_events] CHANGE {uid}")
            log.debug(f"  OLD: {json.dumps(prev_map[uid], ensure_ascii=False)}")
            log.debug(f"  NEW: {json.dumps(cur_map[uid], ensure_ascii=False)}")
            changed.append(cur_map[uid])

    for uid in prev_map:
        if uid not in cur_map:
            log.debug(f"[diff_events] REMOVE {uid}")
            removed.append(prev_map[uid])

    return added, changed, removed

def save_current(cur):
    """Ghi sự kiện hiện tại vào cache"""
    STORE_FILE.parent.mkdir(parents=True, exist_ok=True)  # 👈 đảm bảo thư mục .cache tồn tại
    STORE_FILE.write_text(json.dumps(cur, indent=2, ensure_ascii=False))
    log.info(f"💾 Đã lưu snapshot {len(cur)} sự kiện vào {STORE_FILE}")

def build_output(events, added, changed, removed):
    lines1 = [bold_md("📋 Tất cả lịch sắp tới:")]
    for i, ev in enumerate(events, 1):
        lines1.append(bold_md(f"{i}. 🕐 {fmt_time_range(ev)}"))
        lines1.append(escape_md(f"   📌 Nội dung: {ev['summary']}"))
        lines1.append(escape_md(f"   📍 Địa điểm: {ev['location']}"))
        lines1.append(escape_md(f"   👤 Chủ trì: {get_chu_tri(ev['desc_raw'])}"))
        lines1.append(escape_md("---"))
    part1 = "\n".join(lines1)

    lines2 = [bold_md("🔄 Thay đổi so với lần trước:")]
    index = 1
    for ev in added:
        lines2.append(escape_md(f"{index}. 🆕 [Thêm] {fmt_time_range(ev)} – {ev['summary']}")); index += 1
    for ev in changed:
        lines2.append(escape_md(f"{index}. ✏️ [Sửa] {fmt_time_range(ev)} – {ev['summary']}")); index += 1
    for ev in removed:
        lines2.append(escape_md(f"{index}. ❌ [Xoá] {fmt_time_range(ev)} – {ev['summary']}")); index += 1
    if index == 1:
        lines2.append("(không có thay đổi)")
    part2 = "\n".join(lines2)

    lines3 = [bold_md("📝 Chi tiết các lịch sắp tới:")]
    for ev in events:
        time = fmt_time_range(ev)
        title = ev["summary"]
        desc = parse_description(ev["desc_raw"])
        lines3.append(bold_md(f"[{time}] {title}"))
        lines3.extend(desc)
        lines3.append(escape_md("---\n"))
    part3 = "\n".join(lines3)

    return f"{part1}\n\n{part2}\n\n{part3}"

async def main():
    try:
        current = fetch_events(7)

        if current is None:
            log.warning("⛔ Không thể lấy dữ liệu CalDAV – Dừng xử lý.")
            return

        previous = load_previous()
        added, changed, removed = diff_events(previous, current)
        
        if not (added or removed or changed):
            log.info("✅ Không có thay đổi, không gửi Telegram.")
            return

        text = build_output(current, added, changed, removed)
        #log.info("======= TEXT SẼ GỬI TELEGRAM =======\n%s", text)
        bot = Bot(token=TG_TOKEN)
        await bot.send_message(chat_id=TG_CHAT_ID, text=text, parse_mode="MarkdownV2")

        save_current(current)
        log.info("✅ Đã gửi Telegram (%d mới / %d sửa / %d xoá)", len(added), len(changed), len(removed))

    except Exception as e:
        log.exception("❌ Lỗi trong hàm main()")

if __name__ == "__main__":
    asyncio.run(main())