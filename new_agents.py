import argparse
import hashlib
import html
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

USER_AGENT = "ArenaNewsTelegramAgent/1.0"
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "ref",
    "taid",
}


@dataclass
class NewsItem:
    source: str
    title: str
    link: str
    summary: str
    published_at: Optional[datetime]
    uid: str


class Config:
    def __init__(self) -> None:
        load_dotenv()
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.timezone = os.getenv("TIMEZONE", "America/New_York").strip()
        self.lookback_hours = int(os.getenv("LOOKBACK_HOURS", "24"))
        self.max_items = int(os.getenv("MAX_ITEMS", "15"))
        self.enable_article_snippets = os.getenv("ENABLE_ARTICLE_SNIPPETS", "true").lower() == "true"
        self.article_snippet_limit = int(os.getenv("ARTICLE_SNIPPET_LIMIT", "8"))
        self.send_empty_digest = os.getenv("SEND_EMPTY_DIGEST", "false").lower() == "true"
        self.request_timeout = int(os.getenv("REQUEST_TIMEOUT", "15"))
        self.exclude_url_keywords = split_csv_env("EXCLUDE_URL_KEYWORDS", "sports,outkick,/video/")
        self.exclude_title_keywords = split_csv_env("EXCLUDE_TITLE_KEYWORDS", "newsletter")
        self.sources_file = Path(os.getenv("SOURCES_FILE", "sources.json"))
        self.state_file = Path(os.getenv("STATE_FILE", "state.json"))

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def split_csv_env(name: str, default: str) -> List[str]:
    raw = os.getenv(name, default)
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    filtered_query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in TRACKING_PARAMS]
    clean_parts = (parts.scheme, parts.netloc, parts.path, urlencode(filtered_query), "")
    return urlunsplit(clean_parts)


def clean_text(value: str) -> str:
    if not value:
        return ""

    value = html.unescape(str(value))
    if "<" not in value and ">" not in value:
        return re.sub(r"\s+", " ", value).strip()

    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = date_parser.parse(value)
        except Exception:
            return None
    elif hasattr(value, "tm_year"):
        try:
            dt = datetime(
                value.tm_year,
                value.tm_mon,
                value.tm_mday,
                value.tm_hour,
                value.tm_min,
                value.tm_sec,
                tzinfo=timezone.utc,
            )
        except Exception:
            return None
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_sources(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Sources file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("sources.json must contain a JSON list")
    sources: List[Dict[str, str]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", "")).strip()
        feed_url = str(row.get("feed_url", "")).strip()
        if name and feed_url:
            sources.append({"name": name, "feed_url": feed_url})
    if not sources:
        raise ValueError("No valid feed sources found in sources.json")
    return sources


def fetch_feed_items(session: requests.Session, source: Dict[str, str], timeout: int) -> List[NewsItem]:
    response = session.get(source["feed_url"], timeout=timeout)
    response.raise_for_status()
    feed = feedparser.parse(response.content)

    items: List[NewsItem] = []
    for entry in feed.entries:
        title = clean_text(entry.get("title", ""))
        link = normalize_url(entry.get("link", ""))
        if not title or not link:
            continue

        published_at = (
            parse_datetime(entry.get("published"))
            or parse_datetime(entry.get("updated"))
            or parse_datetime(entry.get("created"))
            or parse_datetime(entry.get("published_parsed"))
            or parse_datetime(entry.get("updated_parsed"))
            or parse_datetime(entry.get("created_parsed"))
        )

        summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
        if summary.lower() == title.lower():
            summary = ""

        uid = stable_hash(f"{source['name']}|{link}|{title.lower()}")
        items.append(
            NewsItem(
                source=source["name"],
                title=title,
                link=link,
                summary=summary,
                published_at=published_at,
                uid=uid,
            )
        )

    return items


def extract_article_snippet(session: requests.Session, url: str, timeout: int) -> str:
    try:
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
    except Exception:
        return ""

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type:
        return ""

    soup = BeautifulSoup(response.text, "html.parser")

    meta_candidates = [
        soup.find("meta", attrs={"property": "og:description"}),
        soup.find("meta", attrs={"name": "description"}),
        soup.find("meta", attrs={"name": "twitter:description"}),
    ]
    for tag in meta_candidates:
        if tag and tag.get("content"):
            text = clean_text(tag.get("content", ""))
            if len(text) >= 40:
                return limit_text(text, 260)

    for selector in ["article p", "main p", ".article-body p", ".story-body p", ".content p"]:
        nodes = soup.select(selector)
        paragraphs = [clean_text(node.get_text(" ", strip=True)) for node in nodes]
        paragraphs = [p for p in paragraphs if len(p) >= 50]
        if paragraphs:
            return limit_text(" ".join(paragraphs[:2]), 260)

    return ""


def limit_text(text: str, max_len: int) -> str:
    text = clean_text(text)
    if len(text) <= max_len:
        return text
    clipped = text[: max_len - 1].rsplit(" ", 1)[0].strip()
    return f"{clipped}…"


def dedupe_items(items: List[NewsItem]) -> List[NewsItem]:
    seen_links = set()
    seen_titles = set()
    unique_items: List[NewsItem] = []

    for item in items:
        title_key = re.sub(r"\W+", "", item.title.lower())
        if item.link in seen_links or title_key in seen_titles:
            continue
        seen_links.add(item.link)
        seen_titles.add(title_key)
        unique_items.append(item)

    return unique_items


def should_exclude_item(item: NewsItem, config: Config) -> bool:
    title = item.title.lower()
    link = item.link.lower()

    if any(keyword in title for keyword in config.exclude_title_keywords):
        return True
    if any(keyword in link for keyword in config.exclude_url_keywords):
        return True
    return False


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"sent_ids": [], "last_run": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"sent_ids": [], "last_run": None}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    state["sent_ids"] = state.get("sent_ids", [])[-5000:]
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def collect_news(config: Config, session: requests.Session, state: Dict[str, Any]) -> List[NewsItem]:
    sources = load_sources(config.sources_file)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.lookback_hours)
    already_sent = set(state.get("sent_ids", []))

    collected: List[NewsItem] = []
    for source in sources:
        try:
            items = fetch_feed_items(session, source, config.request_timeout)
            logging.info("Fetched %s items from %s", len(items), source["name"])
        except Exception as exc:
            logging.warning("Failed to fetch %s: %s", source["name"], exc)
            continue

        for item in items:
            if item.uid in already_sent:
                continue
            if item.published_at and item.published_at < cutoff:
                continue
            if should_exclude_item(item, config):
                continue
            collected.append(item)

    collected.sort(
        key=lambda item: item.published_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    collected = dedupe_items(collected)[: config.max_items]

    if config.enable_article_snippets:
        enriched = 0
        for item in collected:
            if item.summary and len(item.summary) >= 80:
                continue
            if enriched >= config.article_snippet_limit:
                break
            snippet = extract_article_snippet(session, item.link, config.request_timeout)
            if snippet:
                item.summary = snippet
                enriched += 1

    for item in collected:
        item.summary = limit_text(item.summary, 260)

    return collected


def format_item(item: NewsItem, tzinfo: ZoneInfo, index: int) -> str:
    if item.published_at:
        published = item.published_at.astimezone(tzinfo).strftime("%b %d, %I:%M %p")
    else:
        published = "Time unavailable"

    parts = [
        f"{index}. <b>{html.escape(item.title)}</b>",
        f"<i>{html.escape(item.source)} • {html.escape(published)}</i>",
        f'<a href="{html.escape(item.link, quote=True)}">Read more</a>',
    ]
    return "\n".join(parts) + "\n"


def build_messages(items: List[NewsItem], config: Config) -> List[str]:
    now_local = datetime.now(config.tzinfo)
    header = (
        "🗞️ <b>US News Digest</b>\n"
        f"<i>Last {config.lookback_hours} hours • generated {now_local.strftime('%Y-%m-%d %I:%M %p %Z')}</i>\n"
    )

    if not items:
        if config.send_empty_digest:
            return [header + "\nNo new items found in this cycle."]
        return []

    messages = [header]
    for idx, item in enumerate(items, start=1):
        block = "\n" + format_item(item, config.tzinfo, idx)
        if len(messages[-1]) + len(block) > 3500:
            messages.append(block.strip())
        else:
            messages[-1] += block
    return messages


def send_telegram_messages(session: requests.Session, config: Config, messages: List[str]) -> None:
    if not config.telegram_bot_token:
        raise ValueError("Missing TELEGRAM_BOT_TOKEN in environment or .env")
    if not config.telegram_chat_id:
        raise ValueError("Missing TELEGRAM_CHAT_ID in environment or .env")

    api_url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    for message in messages:
        payload = {
            "chat_id": config.telegram_chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        response = session.post(api_url, data=payload, timeout=config.request_timeout)
        try:
            data = response.json()
        except Exception:
            data = {"ok": False, "description": response.text}

        if not response.ok or not data.get("ok"):
            raise RuntimeError(f"Telegram send failed: {data}")


def run_once(config: Config, dry_run: bool = False) -> List[NewsItem]:
    session = make_session()
    state = load_state(config.state_file)
    items = collect_news(config, session, state)
    messages = build_messages(items, config)

    if not messages:
        logging.info("No new items to send.")
        return items

    if dry_run:
        logging.info("Dry run enabled. Generated %s Telegram message(s).", len(messages))
        print("\n\n--- MESSAGE PREVIEW ---\n")
        print("\n\n--- NEXT MESSAGE ---\n\n".join(messages))
    else:
        send_telegram_messages(session, config, messages)
        logging.info("Sent %s message(s) to Telegram.", len(messages))

    if items and not dry_run:
        sent_ids = state.get("sent_ids", [])
        sent_ids.extend(item.uid for item in items)
        state["sent_ids"] = sent_ids[-5000:]
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        save_state(config.state_file, state)

    return items


def send_test_message(config: Config, dry_run: bool = False) -> None:
    message = (
        "✅ <b>News agent test message</b>\n"
        f"Timezone: <code>{html.escape(config.timezone)}</code>\n"
        f"Generated at: <code>{datetime.now(config.tzinfo).strftime('%Y-%m-%d %I:%M %p %Z')}</code>"
    )
    if dry_run:
        print(message)
        return
    session = make_session()
    send_telegram_messages(session, config, [message])
    logging.info("Test message sent successfully.")


def parse_send_time(send_time: str) -> Dict[str, int]:
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", send_time.strip())
    if not match:
        raise ValueError("--send-time must be in HH:MM 24-hour format, e.g. 09:00")
    return {"hour": int(match.group(1)), "minute": int(match.group(2))}


def start_daemon(config: Config, send_time: str, dry_run: bool = False) -> None:
    schedule_parts = parse_send_time(send_time)
    scheduler = BlockingScheduler(timezone=config.tzinfo)

    def scheduled_job() -> None:
        try:
            run_once(config, dry_run=dry_run)
        except Exception as exc:
            logging.exception("Scheduled run failed: %s", exc)

    scheduler.add_job(
        scheduled_job,
        CronTrigger(hour=schedule_parts["hour"], minute=schedule_parts["minute"]),
        id="daily_news_digest",
        replace_existing=True,
    )

    logging.info(
        "Scheduler started. Daily digest set for %s %s",
        send_time,
        config.timezone,
    )
    scheduler.start()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape major US news feeds and send a digest to Telegram.")
    parser.add_argument("--run-once", action="store_true", help="Fetch and send one digest immediately.")
    parser.add_argument("--daemon", action="store_true", help="Run continuously and send once per day on schedule.")
    parser.add_argument("--send-time", default="09:00", help="Daily send time in HH:MM 24-hour format for --daemon.")
    parser.add_argument("--test-telegram", action="store_true", help="Send a Telegram test message.")
    parser.add_argument("--dry-run", action="store_true", help="Preview output without sending to Telegram.")
    return parser


def main() -> None:















    
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    parser = build_arg_parser()
    args = parser.parse_args()
    config = Config()

    if args.test_telegram:
        send_test_message(config, dry_run=args.dry_run)
        return

    if args.daemon:
        start_daemon(config, args.send_time, dry_run=args.dry_run)
        return

    # Default behavior is a one-time run if no mode is supplied.
    run_once(config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
