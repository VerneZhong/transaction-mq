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
TIMEOUT = 15
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def room_key(room):
    raw = room.get("jkss") or room.get("href") or "|".join(str(room.get(k, "")) for k in ("name", "layout", "area", "rent", "room"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def parse_property_code(url):
    m = re.search(r"/(\d{2})_(\d{3})(\d)\.html", url)
    if not m:
        raise ValueError(f"Cannot parse UR property code from URL: {url}")
    return m.group(1), m.group(2), m.group(3)


def parse_area(value):
    if value is None:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:㎡|m²|m2)", str(value), re.I)
    return float(m.group(1)) if m else None


def discover_api_urls(session, room_page):
    headers = {"User-Agent": UA, "Accept-Language": "ja-JP,ja;q=0.9"}
    r = session.get(room_page, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    script_urls = []
    for s in soup.find_all("script", src=True):
        u = urljoin(room_page, s["src"])
        if u not in script_urls:
            script_urls.append(u)

    print(f"Diagnostic: fetched room page, {len(script_urls)} external scripts")
    candidates = []
    for script_url in script_urls:
        try:
            js = session.get(script_url, headers=headers, timeout=8).text
        except Exception:
            continue
        if "detail_bukken_room" not in js and "bukken/detail" not in js and "chintai/api" not in js:
            continue
        idx = js.find("detail_bukken_room")
        if idx >= 0:
            snippet = re.sub(r"\s+", " ", js[max(0, idx-250):idx+350])
            print(f"Diagnostic match in {script_url}: {snippet[:700]}")

        for m in re.finditer(r"https?://[^\"'\s)]+/chintai/api/", js):
            candidates.append(m.group(0))
        for m in re.finditer(r"[\"']([^\"']*bukken/detail/detail_bukken_room/?)[\"']", js):
            raw = m.group(1)
            candidates.append(urljoin(room_page, raw))

    out = []
    for u in candidates:
        if u.endswith("detail_bukken_room") or u.endswith("detail_bukken_room/"):
            endpoint = u
        else:
            endpoint = urljoin(u, "bukken/detail/detail_bukken_room/")
        if endpoint not in out:
            out.append(endpoint)
    return out


def post_room_api(session, target, page_index):
    shisya, danchi, shikibetu = parse_property_code(target["url"])
    payload = {
        "shisya": shisya,
        "danchi": danchi,
        "shikibetu": shikibetu,
        "orderByField": "0",
        "orderBySort": "0",
        "pageIndex": str(page_index),
    }
    room_page = target.get("room_url") or target["url"].replace(".html", "_room.html")
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ja-JP,ja;q=0.9",
        "Referer": room_page,
        "X-Requested-With": "XMLHttpRequest",
    }

    discovered = discover_api_urls(session, room_page) if page_index == 0 else []
    endpoints = discovered + [
        "https://www.ur-net.go.jp/chintai/api/bukken/detail/detail_bukken_room/",
        "https://sumai.r6.ur-net.go.jp/chintai/api/bukken/detail/detail_bukken_room/",
        "https://chintai.sumai.ur-net.go.jp/chintai/api/bukken/detail/detail_bukken_room/",
    ]
    endpoints = list(dict.fromkeys(endpoints))
    print(f"Diagnostic API candidates for {target['name']}: {endpoints}")

    failures = []
    for api_url in endpoints:
        try:
            r = session.post(api_url, data=payload, headers=headers, timeout=TIMEOUT, allow_redirects=True)
            ctype = r.headers.get("content-type", "")
            if r.status_code >= 400:
                failures.append(f"{api_url} -> HTTP {r.status_code}")
                continue
            if "json" not in ctype.lower() and not r.text.lstrip().startswith(("[", "null")):
                failures.append(f"{api_url} -> non-JSON {ctype or 'unknown'}")
                continue
            data = r.json()
            if data is None or isinstance(data, list):
                print(f"{target['name']}: API OK via {api_url}")
                return data
            failures.append(f"{api_url} -> unexpected {type(data).__name__}")
        except Exception as e:
            failures.append(f"{api_url} -> {type(e).__name__}: {e}")

    raise RuntimeError("; ".join(failures))


def fetch_vacant_rooms(session, target, layouts, min_area):
    rooms = []
    seen_ids = set()
    for page_index in range(20):
        data = post_room_api(session, target, page_index)
        if not data:
            break
        new_ids = 0
        reported_total = None
        for item in data:
            if not isinstance(item, dict):
                continue
            if reported_total is None:
                try:
                    reported_total = int(item.get("allCount"))
                except (TypeError, ValueError):
                    reported_total = None
            room_id = str(item.get("id") or "").strip()
            if not room_id or room_id in seen_ids:
                continue
            seen_ids.add(room_id)
            new_ids += 1
            layout = str(item.get("type") or "").strip()
            area = parse_area(item.get("floorspace"))
            if layout not in layouts or area is None or area < min_area:
                continue
            room_link = item.get("roomDetailLink") or item.get("roomDetailLinkSp")
            href = urljoin("https://www.ur-net.go.jp", room_link) if room_link else f"{target.get('room_url')}?JKSS={room_id}"
            rooms.append({"name": target["name"], "room": str(item.get("name") or "").strip(), "layout": layout, "area": area, "rent": str(item.get("rent") or "").strip(), "commonfee": str(item.get("commonfee") or "").strip(), "floor": str(item.get("floor") or "").strip(), "href": href, "jkss": room_id})
        if new_ids == 0:
            break
        if reported_total is not None and len(seen_ids) >= reported_total:
            break
    print(f"{target['name']}: found {len(rooms)} matching vacant rooms")
    return rooms


def telegram_credentials():
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()


def telegram_send_room(room):
    token, chat_id = telegram_credentials()
    if not token or not chat_id:
        return False
    fee = f"（共益費 {room['commonfee']}）" if room.get("commonfee") else ""
    room_name = f" {room['room']}" if room.get("room") else ""
    floor = f" / {room['floor']}" if room.get("floor") else ""
    message = "\n".join(["🏠 UR 新空房提醒", "", f"{room['name']}{room_name} — {room['layout']}", f"面积：{room['area']}㎡{floor}", f"租金：{room.get('rent') or '官网确认'}{fee}", "", "先着順です。条件を確認して、対応可能ならすぐ仮申込してください。"])
    payload = {"chat_id": chat_id, "text": message, "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": [[{"text": "🏠 立即查看・仮申込", "url": room["href"]}]]}}
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    if not r.json().get("ok"):
        raise RuntimeError("Telegram API rejected message")
    return True


def format_notice(new_rooms):
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    lines = ["# 🏠 UR 新空房提醒", "", f"检查时间：{now}", ""]
    for r in new_rooms:
        fee = f"（共益費 {r['commonfee']}）" if r.get("commonfee") else ""
        room = f" {r['room']}" if r.get("room") else ""
        floor = f" / {r['floor']}" if r.get("floor") else ""
        lines += [f"## {r['name']}{room} — {r['layout']}", f"- 面积：{r['area']}㎡{floor}", f"- 租金：{r.get('rent') or '官网确认'}{fee}", f"- UR：{r['href']}", ""]
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
            try:
                current_rooms.extend(fetch_vacant_rooms(session, target, layouts, min_area))
            except Exception as e:
                errors.append(f"{target['name']}: {e}")
    if errors:
        print("UR vacancy check failed; preserving previous state.", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 1
    unique = {room_key(r): r for r in current_rooms}
    current_keys = set(unique)
    new_rooms = [unique[k] for k in sorted(current_keys - previous)]
    notify_rooms = new_rooms if bool(state.get("initialized")) else []
    STATE_FILE.write_text(json.dumps({"initialized": True, "checked_at": datetime.now().astimezone().isoformat(), "room_keys": sorted(current_keys), "rooms": list(unique.values()), "errors": []}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
