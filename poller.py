#!/usr/bin/env python3

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
from dotenv import load_dotenv


# ============================================================
# Configuration
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", BASE_DIR / "config.json"))
STATE_PATH = Path(os.getenv("STATE_PATH", BASE_DIR / "state.json"))

load_dotenv(BASE_DIR / ".env")


def expand_dates(cfg):
    """
    Build the list of dates (YYYYMMDD) to check. Supported, in priority order:

      env REQUESTED_DATES = "20261219,20261220"  or  "20261219-20261220"
      config "requested_dates": ["20261219", "20261220"]
      config "date_from": "20261219", "date_to": "20261220"
      config "requested_date": "20261219"   (single date)
    """
    env_value = os.getenv("REQUESTED_DATES") or os.getenv("REQUESTED_DATE")

    if env_value:
        env_value = env_value.strip()

        if "-" in env_value:
            start, end = [p.strip() for p in env_value.split("-", 1)]
            return date_range(start, end)

        return [d.strip() for d in env_value.split(",") if d.strip()]

    if cfg.get("requested_dates"):
        return list(cfg["requested_dates"])

    if cfg.get("date_from") and cfg.get("date_to"):
        return date_range(cfg["date_from"], cfg["date_to"])

    if cfg.get("requested_date"):
        return [cfg["requested_date"]]

    raise ValueError(
        "No dates configured. Set requested_dates, "
        "date_from/date_to, or requested_date."
    )


def date_range(start, end):
    """Inclusive list of YYYYMMDD strings from start to end."""
    d1 = datetime.strptime(start, "%Y%m%d")
    d2 = datetime.strptime(end, "%Y%m%d")

    if d2 < d1:
        raise ValueError(f"date range end {end} is before start {start}")

    days = (d2 - d1).days

    return [
        (d1 + timedelta(days=i)).strftime("%Y%m%d")
        for i in range(days + 1)
    ]


def pretty_date(date_code):
    """'20261219' -> 'Sat, 19 Dec 2026'."""
    try:
        return datetime.strptime(date_code, "%Y%m%d").strftime("%a, %d %b %Y")
    except ValueError:
        return date_code


def url_for_date(cfg, date_code):
    """Movie page URL for a given date."""
    if cfg.get("url_template"):
        return cfg["url_template"].format(date=date_code)

    base = os.getenv("TARGET_URL") or cfg["target_url"]

    return re.sub(r"/\d{8}$", "/" + date_code, base)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    cfg["movie"] = os.getenv("MOVIE", cfg["movie"])

    cfg["dates"] = expand_dates(cfg)

    cfg["telegram_bot_token"] = os.getenv(
        "TELEGRAM_BOT_TOKEN",
        cfg.get("telegram_bot_token", "")
    )

    cfg["telegram_chat_id"] = os.getenv(
        "TELEGRAM_CHAT_ID",
        cfg.get("telegram_chat_id", "")
    )

    # Only English formats by default.
    cfg["languages"] = cfg.get("languages") or ["English"]

    # Venues to match: by venue code, or by venue name (venue_labels).
    cfg["venue_codes"] = cfg.get("venue_codes") or []
    cfg["venue_labels"] = cfg.get("venue_labels") or {}

    return cfg


def cfg_for_date(cfg, date_code):
    """Copy of cfg pointed at a single date."""
    dated = dict(cfg)
    dated["requested_date"] = date_code
    dated["target_url"] = url_for_date(cfg, date_code)

    return dated


# ============================================================
# State (so we only alert on newly opened venues)
# ============================================================

def load_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            print("WARNING: state.json unreadable, starting fresh.")

    return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def state_key(date_code, format_code, venue_code):
    return f"{date_code}:{format_code}:{venue_code}"


# ============================================================
# Fetch BookMyShow page
# ============================================================

def fetch(cfg, url=None):
    url = url or cfg["target_url"]

    scrape_do_token = os.getenv("SCRAPE_DO_TOKEN")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,image/avif,"
            "image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    if scrape_do_token:
        print("Using Scrape.do (India)")

        response = requests.get(
            "https://api.scrape.do/",
            params={
                "token": scrape_do_token,
                "url": url,
                "geoCode": "in"
            },
            headers=headers,
            timeout=90
        )

    else:
        print("Using direct request")

        response = requests.get(
            url,
            headers=headers,
            timeout=45
        )

    print(f"HTTP status: {response.status_code}")

    response.raise_for_status()

    return response


# ============================================================
# Extract BookMyShow initial state
# ============================================================

def extract_state(page):
    html = page.text

    marker = "window.__INITIAL_STATE__"

    marker_position = html.find(marker)

    if marker_position == -1:
        raise ValueError(
            "window.__INITIAL_STATE__ not found in page"
        )

    start = html.find("{", marker_position + len(marker))

    if start == -1:
        raise ValueError(
            "Could not find JSON object in initial state"
        )

    decoder = json.JSONDecoder()

    state, _ = decoder.raw_decode(html[start:])

    return state


# ============================================================
# JSON helpers
# ============================================================

def walk(node):
    """Yield every dict in a nested JSON structure."""
    if isinstance(node, dict):
        yield node

        for value in node.values():
            yield from walk(value)

    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


# ============================================================
# Find format selectors
# ============================================================

def find_format_selector(state):
    selectors = []

    for node in walk(state):
        value = node.get("format-selector")

        if isinstance(value, dict):
            selectors.append(value)

    return selectors


# ============================================================
# Construct format-specific URL
# ============================================================

def build_format_url(cfg, event_code, event_slug):
    parts = urlsplit(cfg["target_url"])

    segments = parts.path.split("/")

    try:
        movies_index = segments.index("movies")

        if segments[movies_index + 3] != "buytickets":
            raise ValueError(
                "Unexpected BookMyShow URL structure"
            )

        # Replace movie slug, event code and date.
        segments[movies_index + 2] = event_slug
        segments[movies_index + 4] = event_code
        segments[movies_index + 5] = cfg["requested_date"]

    except (ValueError, IndexError):
        raise ValueError(
            f"Cannot construct format URL from: {cfg['target_url']}"
        )

    return urlunsplit((
        parts.scheme,
        parts.netloc,
        "/".join(segments),
        parts.query,
        parts.fragment
    ))


# ============================================================
# Extract all formats for the configured languages
# ============================================================

def extract_formats(state, cfg):
    selectors = find_format_selector(state)

    if not selectors:
        print("WARNING: format-selector not found.")
        return []

    wanted_languages = [lang.lower() for lang in cfg["languages"]]

    formats = []
    seen = set()

    for selector in selectors:

        widgets = selector.get("widgets", [])

        for widget in widgets:

            if widget.get("type") != "chip-list":
                continue

            language = widget.get("text", "Unknown")

            # Language filter (English only by default).
            if language.lower() not in wanted_languages:
                continue

            for chip in widget.get("data", []):

                title = chip.get("title")

                cta = chip.get("cta", {})
                additional = cta.get("additionalData", {})

                event_code = additional.get("eventCode")
                event_slug = additional.get("eventUrl")

                if not title or not event_code or not event_slug:
                    continue

                # Ignore the all-formats option.
                if event_code == "*":
                    continue

                # isDisabled is NOT used to skip formats: BookMyShow
                # sets it on the format currently being viewed.

                key = (event_code, language)

                if key in seen:
                    continue

                seen.add(key)

                url = build_format_url(
                    cfg,
                    event_code,
                    event_slug
                )

                formats.append({
                    "title": title,
                    "language": language,
                    "code": event_code,
                    "url": url
                })

    return formats


# ============================================================
# Venue detection (parses JSON, not page text)
# ============================================================

def venue_is_wanted(code, name, cfg):
    """True if no venues configured, or code/name matches config."""
    if not cfg["venue_codes"] and not cfg["venue_labels"]:
        return True

    if code in cfg["venue_codes"]:
        return True

    labels = {label.lower() for label in cfg["venue_labels"].values()}

    return name.lower() in labels


def check_venues(page, cfg):
    state = extract_state(page)

    date = cfg["requested_date"]

    found = {}
    cards = 0

    for card in walk(state):

        if card.get("type") != "venue-card":
            continue

        cards += 1

        additional = card.get("additionalData", {})
        code = additional.get("venueCode", "")
        name = additional.get("venueName", code)

        if not venue_is_wanted(code, name, cfg):
            continue

        print(f"  [venue] {code} | {name}")

        shows = []

        for section in card.get("showtimesSections", []):

            for showtime in section.get("showtimes", []):

                show_data = showtime.get("additionalData", {})
                show_time = show_data.get("showTime", "?")

                # BookMyShow silently falls back to another date
                # when the requested one isn't open. Ignore those.
                if show_data.get("showDateCode") != date:
                    print(
                        f"    [reject] {show_time}: date "
                        f"{show_data.get('showDateCode')} != {date}"
                    )
                    continue

                # availStatus "0" means sold out.
                if show_data.get("availStatus") == "0":
                    print(f"    [reject] {show_time}: sold out")
                    continue

                print(f"    [ok] {show_time}")

                label = show_time

                if show_data.get("attributes"):
                    label += f" ({show_data['attributes']})"

                shows.append(label)

        if shows:
            found[code] = {
                "name": name,
                "shows": shows
            }

    print(f"  venue cards on page: {cards}")

    return found


# ============================================================
# Telegram notification
# ============================================================

def send_telegram(cfg, message):
    token = cfg.get("telegram_bot_token")
    chat_id = cfg.get("telegram_chat_id")

    if not token or not chat_id:
        raise ValueError(
            "Telegram bot token or chat ID is missing"
        )

    telegram_url = (
        f"https://api.telegram.org/bot{token}/sendMessage"
    )

    response = requests.post(
        telegram_url,
        data={
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True
        },
        timeout=30
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {result}"
        )

    print("Telegram notification sent successfully")


# ============================================================
# Check every format (for one date)
# ============================================================

def check_formats(cfg, formats, first_page):
    """Returns (results, all_ok). all_ok is False if any format failed."""
    results = []
    all_ok = True

    for fmt in formats:

        title = fmt["title"]
        language = fmt["language"]
        url = fmt["url"]

        print("\n" + "=" * 50)
        print(f"Checking: {language} - {title}")
        print(f"Event code: {fmt['code']}")
        print(f"URL: {url}")

        try:
            # Reuse the first page for the format already loaded.
            if fmt["code"] in cfg["target_url"]:
                print("Reusing already fetched page")
                page = first_page
            else:
                page = fetch(cfg, url)

            venues = check_venues(page, cfg)

            if venues:

                print("\nMatching venues:")

                for code, venue in venues.items():
                    print(
                        f"FOUND: {code} - {venue['name']}: "
                        f"{', '.join(venue['shows'])}"
                    )

                results.append({
                    "title": title,
                    "language": language,
                    "code": fmt["code"],
                    "url": url,
                    "date": cfg["requested_date"],
                    "venues": venues
                })

            else:
                print("No open shows at configured venues.")

        except Exception as exc:
            all_ok = False

            print(
                f"ERROR checking {title}: {exc}",
                file=sys.stderr
            )

    return results, all_ok


# ============================================================
# Process one date
# ============================================================

def process_date(cfg, date_code):
    """Returns (results, all_ok)."""
    dcfg = cfg_for_date(cfg, date_code)

    print("\n" + "#" * 60)
    print(f"DATE: {pretty_date(date_code)} ({date_code})")
    print(f"URL: {dcfg['target_url']}")
    print("#" * 60)

    first_page = fetch(dcfg)

    state = extract_state(first_page)

    # Formats can differ per date, so discover them for each date.
    formats = extract_formats(state, dcfg)

    if not formats:
        print("\nNo formats extracted for this date.")
        return [], True

    print(f"\nFound {len(formats)} format(s):")

    for fmt in formats:
        print(
            f"  {fmt['language']} - {fmt['title']} "
            f"({fmt['code']})"
        )

    return check_formats(dcfg, formats, first_page)


# ============================================================
# Main
# ============================================================

def main():
    cfg = load_config()

    print("=" * 60)
    print(f"Movie: {cfg['movie']}")
    print(f"Dates: {', '.join(pretty_date(d) for d in cfg['dates'])}")
    print(f"Languages: {', '.join(cfg['languages'])}")
    print(f"Venues: {cfg['venue_codes'] or 'any'}")
    print("=" * 60)

    prev = load_state()
    current = {}
    notify_failed = False

    for date_code in cfg["dates"]:

        try:
            results, ok = process_date(cfg, date_code)

        except Exception as exc:
            # One bad date shouldn't stop the others.
            print(
                f"ERROR processing {date_code}: {exc}",
                file=sys.stderr
            )
            results, ok = [], False

        # If anything failed for this date, keep its previous state so a
        # temporary error isn't mistaken for "sold out" and re-alerted.
        if not ok:
            for key, value in prev.items():
                if key.startswith(date_code + ":"):
                    current[key] = value

        for result in results:

            new_venues = {}

            for vcode, venue in result["venues"].items():
                key = state_key(result["date"], result["code"], vcode)
                current[key] = True

                if key not in prev:
                    new_venues[vcode] = venue

            if not new_venues:
                print(
                    f"No new venues for {result['title']} "
                    f"on {result['date']} (already alerted)."
                )
                continue

            message = (
                "🎟️ Booking OPEN on BookMyShow!\n\n"
                f"Movie: {cfg['movie']}\n"
                f"Date: {pretty_date(result['date'])}\n"
                f"Language: {result['language']}\n"
                f"Format: {result['title']}\n\n"
                + "\n".join(
                    f"📍 {venue['name']}\n"
                    + "\n".join(
                        f"   • {show}" for show in venue["shows"][:8]
                    )
                    for venue in new_venues.values()
                )
                + f"\n\n{result['url']}"
            )

            try:
                send_telegram(cfg, message)
            except Exception as exc:
                # Don't mark as alerted if the message didn't go out.
                notify_failed = True
                print(f"ERROR sending Telegram: {exc}", file=sys.stderr)
                for vcode in new_venues:
                    current.pop(
                        state_key(result["date"], result["code"], vcode),
                        None
                    )

    save_state(current)

    if not current:
        print("\nNo open shows at configured venues on any date/format.")

    print("\nRun completed.")

    return 1 if notify_failed else 0


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":

    try:
        sys.exit(main())

    except Exception as exc:

        print(
            f"ERROR: {exc}",
            file=sys.stderr
        )

        sys.exit(1)