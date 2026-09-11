import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
CONFIG_FILE = ROOT / "config.yml"
OUTPUT_FILE = ROOT / "notification.md"
UA = "Mozilla/5.0 (compatible; UR-House-Watcher/1.0; +https://github.com/)"
TIMEOUT = 25


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def normalize(text):
    return re.sub(r"\s+", " ", text or "").strip()


def room_key(room):
    raw = "|".join(str(room.get(k, "")) for k in ("name", "layout", "area", "rent", "href", "text"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def parse_number(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:㎡|m²|m2)", text, re.I)
    return float(m.group(1)) if m else None


def extract_rooms(html, base_url, name, layouts, min_area):
    soup = BeautifulSoup(html, "html.parser")
    rooms = []
    seen = set()

    # UR can change markup. Instead of depending on one CSS class, inspect
    # compact ancestor blocks around links/text that contain a target layout.
    for node in soup.find_all(string=True):
        text = normalize(str(node))
        if not any(layout.lower() in text.lower() for layout in layouts):
            continue

        block = node.parent
        for _ in range(5):
            if not block or not getattr(block, "get_text", None):
                break
            block_text = normalize(block.get_text(" ", strip=True))
            if 20 <= len(block_text) <= 900:
                area = parse_number(block_text)
                if area is None or area >= min_area:
                    layout = next((x for x in layouts if x.lower() in block_text.lower()), None)
                    rent_match = re.search(r"(?:賃料)?\s*([\d,]+)\s*円", block_text)
                    rent = rent_match.group(1) + "円" if rent_match else ""
                    link = block.find("a", href=True)
                    href = urljoin(base_url, link["href"]) if link else base_url
                    room = {
                        "name": name,
                        "layout": layout,
                        "area": area,
                        "rent": rent,
                        "href": href,
                        "text": block_text[:500],
                    }
                    key = room_key(room)
                    if key not in seen:
                        seen.add(key)
                        rooms.append(room)
                break
            block = block.parent

    return rooms


def fetch(session, url):
    r = session.get(url, timeout=TIMEOUT, headers={"User-Agent": UA, "Accept-Language": "ja-JP,ja;q=0.9"})
    r.raise_for_status()
    return r.text


def telegram_send(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "disable_web_page_preview": False},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return True


def format_notice(new_rooms):
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    lines = ["# 🏠 UR 新空房提醒", "", f"检查时间：{now}", ""]
    for r in new_rooms:
        area = f"{r['area']}㎡" if r.get("area") is not None else "面积未解析"
        rent = r.get("rent") or "租金请打开官网确认"
        lines += [f"## {r['name']} — {r.get('layout') or '目标户型'}", f"- 面积：{area}", f"- 租金：{rent}", f"- UR：{r['href']}", ""]
    lines += ["> UR 房源先着顺。收到提醒后建议立即打开官网确认。"]
    return "\n".join(lines)


def main():
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    layouts = cfg["layouts"]
    min_area = float(cfg["min_area_sqm"])
    state = load_json(STATE_FILE, {})
    previous = set(state.get("room_keys", []))
    current_rooms = []
    errors = []

    with requests.Session() as session:
        for target in cfg["targets"]:
            urls = [target.get("room_url"), target.get("url")]
            target_rooms = []
            for url in [u for u in urls if u]:
                try:
                    html = fetch(session, url)
                    target_rooms = extract_rooms(html, url, target["name"], layouts, min_area)
                    if target_rooms:
                        break
                except Exception as e:
                    errors.append(f"{target['name']} {url}: {e}")
            current_rooms.extend(target_rooms)

    unique = {room_key(r): r for r in current_rooms}
    current_keys = set(unique)
    new_keys = current_keys - previous
    new_rooms = [unique[k] for k in sorted(new_keys)]

    # First run establishes a baseline and does not spam historical matches.
    initialized = bool(state.get("initialized"))
    notify_rooms = new_rooms if initialized else []

    new_state = {
        "initialized": True,
        "checked_at": datetime.now().astimezone().isoformat(),
        "room_keys": sorted(current_keys),
        "rooms": list(unique.values()),
        "errors": errors,
    }
    STATE_FILE.write_text(json.dumps(new_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if notify_rooms:
        notice = format_notice(notify_rooms)
        OUTPUT_FILE.write_text(notice + "\n", encoding="utf-8")
        try:
            telegram_send(notice.replace("# ", "").replace("## ", ""))
        except Exception as e:
            print(f"Telegram send failed: {e}", file=sys.stderr)
        print(notice)
        return 10

    OUTPUT_FILE.write_text("", encoding="utf-8")
    print(f"No new target rooms. Parsed {len(current_rooms)} matches. Errors: {len(errors)}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
