import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
CONFIG_FILE = ROOT / "config.yml"
OUTPUT_FILE = ROOT / "notification.md"
TIMEOUT = 25


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def normalize(text):
    return re.sub(r"\s+", " ", text or "").strip()


def room_key(room):
    raw = room.get("jkss") or room.get("href") or "|".join(
        str(room.get(k, "")) for k in ("name", "layout", "area", "rent", "room")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def parse_area(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:㎡|m²|m2)", text or "", re.I)
    return float(m.group(1)) if m else None


def chrome_binary():
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError("Chrome/Chromium is not installed on this runner")


def render_page(url):
    """Render the official UR page in headless Chrome so its current JS can load vacancies."""
    cmd = [
        chrome_binary(),
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-extensions",
        "--hide-scrollbars",
        "--window-size=1280,1600",
        "--virtual-time-budget=12000",
        "--dump-dom",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    if proc.returncode != 0:
        err = normalize(proc.stderr)[-800:]
        raise RuntimeError(f"headless Chrome failed ({proc.returncode}): {err}")
    html = proc.stdout
    if len(html) < 1000 or "ur-net.go.jp" not in html.lower():
        raise RuntimeError(f"rendered page looks incomplete ({len(html)} bytes)")
    return html


def extract_room_from_link(link, base_url, target_name, layouts, min_area):
    href = urljoin(base_url, link.get("href", ""))
    qs = parse_qs(urlparse(href).query)
    room_id = (qs.get("JKSS") or qs.get("jkss") or [""])[0].strip()
    if not room_id:
        return None

    # Starting from a real JKSS link, walk upward until we reach the smallest
    # card-like block containing layout and floor area. This avoids the old
    # false positive from the generic layout filter menu.
    block = link
    chosen_text = ""
    for _ in range(10):
        block = getattr(block, "parent", None)
        if block is None or not hasattr(block, "get_text"):
            break
        text = normalize(block.get_text(" ", strip=True))
        area = parse_area(text)
        layout = next((x for x in layouts if re.search(rf"(?<![A-Z0-9]){re.escape(x)}(?![A-Z0-9])", text, re.I)), None)
        if layout and area is not None:
            chosen_text = text
            break

    if not chosen_text:
        return None

    area = parse_area(chosen_text)
    layout = next((x for x in layouts if re.search(rf"(?<![A-Z0-9]){re.escape(x)}(?![A-Z0-9])", chosen_text, re.I)), None)
    if not layout or area is None or area < min_area:
        return None

    room_match = re.search(r"([0-9A-Za-z\-]+号室)", chosen_text)
    rent_match = re.search(r"(?<!共益費)\s*([\d,]{4,})\s*円", chosen_text)
    fee_match = re.search(r"共益費\s*[:：]?\s*([\d,]+\s*円)", chosen_text)
    floor_match = re.search(r"(\d+階(?:\s*/\s*\d+階)?)", chosen_text)

    return {
        "name": target_name,
        "room": room_match.group(1) if room_match else "",
        "layout": layout,
        "area": area,
        "rent": (rent_match.group(1) + "円") if rent_match else "",
        "commonfee": fee_match.group(1).replace(" ", "") if fee_match else "",
        "floor": floor_match.group(1).replace(" ", "") if floor_match else "",
        "href": href,
        "jkss": room_id,
    }


def fetch_vacant_rooms(target, layouts, min_area):
    """Read vacancies from the JS-rendered official UR room-list page."""
    url = target.get("room_url") or target["url"].replace(".html", "_room.html")
    html = render_page(url)
    soup = BeautifulSoup(html, "html.parser")

    # The page is considered healthy only if its core room-list UI was rendered.
    page_text = normalize(soup.get_text(" ", strip=True))
    if target["name"] not in page_text or "部屋情報" not in page_text:
        raise RuntimeError("UR page rendered, but expected room-list content is missing")

    rooms_by_id = {}
    for link in soup.find_all("a", href=True):
        if "JKSS=" not in link["href"] and "jkss=" not in link["href"]:
            continue
        room = extract_room_from_link(link, url, target["name"], layouts, min_area)
        if room:
            rooms_by_id[room["jkss"]] = room

    print(f"{target['name']}: rendered OK, found {len(rooms_by_id)} matching vacant rooms")
    return list(rooms_by_id.values())


def telegram_credentials():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return token, chat_id


def telegram_send_room(room):
    token, chat_id = telegram_credentials()
    if not token or not chat_id:
        return False

    fee = f"（共益費 {room['commonfee']}）" if room.get("commonfee") else ""
    room_name = f" {room['room']}" if room.get("room") else ""
    floor = f" / {room['floor']}" if room.get("floor") else ""
    message = "\n".join(
        [
            "🏠 UR 新空房提醒",
            "",
            f"{room['name']}{room_name} — {room['layout']}",
            f"面积：{room['area']}㎡{floor}",
            f"租金：{room.get('rent') or '官网确认'}{fee}",
            "",
            "先着順です。条件を確認して、対応可能ならすぐ仮申込してください。",
        ]
    )

    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[{"text": "🏠 立即查看・仮申込", "url": room["href"]}]]
        },
    }
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json=payload,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API rejected message: {data}")
    return True


def format_notice(new_rooms):
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    lines = ["# 🏠 UR 新空房提醒", "", f"检查时间：{now}", ""]
    for r in new_rooms:
        fee = f"（共益費 {r['commonfee']}）" if r.get("commonfee") else ""
        room = f" {r['room']}" if r.get("room") else ""
        floor = f" / {r['floor']}" if r.get("floor") else ""
        lines += [
            f"## {r['name']}{room} — {r['layout']}",
            f"- 面积：{r['area']}㎡{floor}",
            f"- 租金：{r.get('rent') or '官网确认'}{fee}",
            f"- UR：{r['href']}",
            "",
        ]
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

    for target in cfg["targets"]:
        try:
            current_rooms.extend(fetch_vacant_rooms(target, layouts, min_area))
        except Exception as e:
            errors.append(f"{target['name']}: {e}")

    # Never replace the last good snapshot when UR cannot be checked.
    if errors:
        print("UR vacancy check failed; preserving previous state.", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 1

    unique = {room_key(r): r for r in current_rooms}
    current_keys = set(unique)
    new_keys = current_keys - previous
    new_rooms = [unique[k] for k in sorted(new_keys)]

    initialized = bool(state.get("initialized"))
    notify_rooms = new_rooms if initialized else []

    new_state = {
        "initialized": True,
        "checked_at": datetime.now().astimezone().isoformat(),
        "room_keys": sorted(current_keys),
        "rooms": list(unique.values()),
        "errors": [],
    }
    STATE_FILE.write_text(json.dumps(new_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if notify_rooms:
        notice = format_notice(notify_rooms)
        OUTPUT_FILE.write_text(notice + "\n", encoding="utf-8")
        for room in notify_rooms:
            try:
                telegram_send_room(room)
            except Exception as e:
                print(f"Telegram send failed for {room.get('href')}: {e}", file=sys.stderr)
        print(notice)
        return 10

    OUTPUT_FILE.write_text("", encoding="utf-8")
    print(f"No new target rooms. Parsed {len(current_rooms)} real matching rooms. Errors: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
