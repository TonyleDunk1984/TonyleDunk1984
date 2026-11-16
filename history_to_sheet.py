#!/usr/bin/env python3
"""Bucket a browser history export into the 15-minute activity sheet.

The Nov 2026 tab has day columns starting at F (Nov 1) and time labels in
column A. Row 5 is 9:00 AM. Slots step by 15 minutes until 8:30 AM the next
morning. Row 61 is a second 10:45 PM label, so that row stays blank and every
later slot moves down one row.

A column is 9:00 AM that calendar day through 8:30 AM the next morning.
A visit at 1:30 AM on Nov 15 lands in the Nov 14 column. The --start/--end
dates select those columns (America/Vancouver).
"""

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("America/Vancouver")
NINE_AM = 9 * 60
# Minutes after 9:00 AM: 10:45 PM, then 11:00 PM through 8:30 AM next morning.
LAST_BEFORE_GAP = 13 * 60 + 45
FIRST_AFTER_GAP = 14 * 60
LAST_SLOT = 23 * 60 + 30

# Row 5 is the first slot. Row 61 (index 56) is the duplicate 10:45 PM.
OFFSETS = (
    list(range(0, LAST_BEFORE_GAP + 1, 15))
    + [None]
    + list(range(FIRST_AFTER_GAP, LAST_SLOT + 1, 15))
)
OFFSET_ROW = {minutes: index for index, minutes in enumerate(OFFSETS) if minutes is not None}

TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %I:%M:%S %p",
    "%Y-%m-%d %I:%M %p",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M",
    "%b %d, %Y, %I:%M:%S %p",
    "%b %d, %Y, %I:%M %p",
    "%b %d, %Y %I:%M %p",
    "%b %d %Y %H:%M",
    "%d %b %Y %H:%M:%S",
    "%d %b %Y %H:%M",
)
TIME_PATTERNS = (
    re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
    re.compile(r"\d{1,2}/\d{1,2}/\d{4}[, T]+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?", re.I),
    re.compile(
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2},? \d{4},? \d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?",
        re.I,
    ),
)
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")


def read_text(path):
    raw = Path(path).read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("utf-16")


def domain_of(url):
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or "unknown"


def parse_numeric_time(number):
    number = float(number)
    if number > 10**16:  # Chrome/WebKit microseconds since 1601-01-01 UTC
        seconds = number / 1_000_000 - 11_644_473_600
    elif number > 10**14:  # Unix microseconds (Chrome time_usec, Firefox PRTime)
        seconds = number / 1_000_000
    elif number > 10**11:  # Unix milliseconds
        seconds = number / 1_000
    else:
        seconds = number
    return datetime.fromtimestamp(seconds, tz=ZoneInfo("UTC")).astimezone(ZONE)


def parse_when(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return parse_numeric_time(value)
    text = str(value).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return parse_numeric_time(text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
        for fmt in TIME_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError(f"unrecognized time: {value!r}")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ZONE)
    return parsed.astimezone(ZONE)


def _field(record, *names):
    folded = {re.sub(r"[\s_]+", "", key).lower(): key for key in record}
    for name in names:
        key = folded.get(re.sub(r"[\s_]+", "", name))
        if key is not None and str(record[key]).strip():
            return record[key]
    return None


def visit_from_record(record):
    url = _field(record, "url", "uri", "address", "link")
    when = _field(
        record,
        "time_usec",
        "visit_time",
        "visit time",
        "last_visit_time",
        "last visit time",
        "datetime",
        "timestamp",
        "date_time",
        "last_visit_date",
        "visited",
        "time",
        "date",
    )
    if not url or when is None:
        return None
    url = str(url).strip()
    if not url.lower().startswith(("http://", "https://")):
        return None
    title = _field(record, "title", "name", "page_title", "page title") or ""
    try:
        return parse_when(when), str(title).strip(), url
    except (ValueError, OSError, OverflowError):
        return None


def parse_json(text):
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = []
        for line in text.splitlines():
            piece = line.strip().rstrip(",")
            if not piece or piece in "[]{}":
                continue
            data.append(json.loads(piece))

    def lists_of_visits(node):
        if isinstance(node, list):
            rows = [item for item in node if isinstance(item, dict) and visit_from_record(item)]
            if rows:
                return rows
            found = []
            for item in node:
                found.extend(lists_of_visits(item))
            return found
        if isinstance(node, dict):
            found = []
            for value in node.values():
                found.extend(lists_of_visits(value))
            return found
        return []

    visits = []
    for record in lists_of_visits(data):
        parsed = visit_from_record(record)
        if parsed:
            visits.append(parsed)
    return visits


def parse_csv(text):
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        return []
    visits = []
    for record in reader:
        parsed = visit_from_record(record)
        if parsed:
            visits.append(parsed)
    return visits


def parse_text(text):
    visits = []
    for line in text.splitlines():
        url_match = URL_PATTERN.search(line)
        if not url_match:
            continue
        when_text = None
        for pattern in TIME_PATTERNS:
            found = pattern.search(line)
            if found:
                when_text = found.group(0)
                break
        if not when_text:
            continue
        title = line
        title = title.replace(url_match.group(0), " ")
        title = title.replace(when_text, " ")
        title = re.sub(r"[\t|,]+", " ", title)
        title = re.sub(r"\s+", " ", title).strip(" -–—")
        try:
            when = parse_when(when_text)
        except (ValueError, OSError, OverflowError):
            continue
        visits.append((when, title, url_match.group(0).rstrip(").,;")))
    return visits


def load_visits(path):
    text = read_text(path).strip()
    if not text:
        return []
    first = text.lstrip()[:1]
    if first in "{[":
        try:
            return parse_json(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path} looks like JSON but did not parse: {exc}") from exc
    header = text.splitlines()[0].lower()
    if ("," in header or "\t" in header or ";" in header) and any(
        word in header for word in ("url", "title", "date", "time", "visit")
    ):
        return parse_csv(text)
    return parse_text(text)


def load_rules(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    rules = data.get("rules", data if isinstance(data, list) else [])
    if not isinstance(rules, list):
        raise SystemExit(f"{path} has no rules list")
    return rules


def match_label(title, url, rules):
    host = domain_of(url)
    folded_title = title.casefold()
    folded_url = url.casefold()
    for rule in rules:
        label = rule.get("label")
        if not label:
            continue
        for keyword in rule.get("title") or []:
            if keyword.casefold() in folded_title:
                return label
        for fragment in rule.get("url") or []:
            if fragment.casefold() in folded_url:
                return label
        for domain in rule.get("domains") or []:
            needle = domain.casefold().removeprefix("www.")
            if host == needle or host.endswith("." + needle):
                return label
    return None


def sheet_day_and_offset(when):
    local = when.astimezone(ZONE)
    minutes = local.hour * 60 + local.minute
    if minutes < NINE_AM:
        day = local.date() - timedelta(days=1)
        offset = (24 * 60 - NINE_AM) + minutes
    else:
        day = local.date()
        offset = minutes - NINE_AM
    return day, offset - (offset % 15)


def choose_label(bucket, rules):
    """Most visits wins. Equal counts go to the category seen earlier."""
    counts = defaultdict(int)
    first_seen = {}
    unknown_counts = defaultdict(int)
    unknown_seen = {}
    for when, title, url in sorted(bucket, key=lambda item: item[0]):
        label = match_label(title, url, rules)
        if label is None:
            host = domain_of(url)
            unknown_counts[host] += 1
            unknown_seen.setdefault(host, when)
        else:
            counts[label] += 1
            first_seen.setdefault(label, when)
    if counts:
        return min(counts, key=lambda label: (-counts[label], first_seen[label]))
    if not unknown_counts:
        return ""
    host = min(unknown_counts, key=lambda name: (-unknown_counts[name], unknown_seen[name]))
    return f"?? {host}"


def column_letter(day):
    """Nov 2026 layout: column F is the 1st, G the 2nd, and so on."""
    number = 5 + day.day  # F is the 6th column
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def clock(when):
    local = when.astimezone(ZONE)
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'} {local.strftime('%b')} {local.day}"


def build_days(visits, rules, start, end):
    slots = defaultdict(list)
    in_column = defaultdict(list)
    for when, title, url in visits:
        day, offset = sheet_day_and_offset(when)
        if day < start or day > end:
            continue
        in_column[day].append(when)
        if offset in OFFSET_ROW:
            slots[(day, offset)].append((when, title, url))
    days = []
    cursor = start
    while cursor <= end:
        labels = []
        hours = defaultdict(float)
        for offset in OFFSETS:
            if offset is None:
                labels.append("")
                continue
            label = choose_label(slots.get((cursor, offset), []), rules)
            labels.append(label)
            if label:
                hours[label] += 0.25
        times = in_column.get(cursor, [])
        days.append(
            {
                "date": cursor,
                "labels": labels,
                "hours": dict(hours),
                "first": min(times) if times else None,
                "last": max(times) if times else None,
            }
        )
        cursor += timedelta(days=1)
    return days


def print_blocks(days):
    for day in days:
        date = day["date"]
        print(
            f"--- {date.isoformat()} column {column_letter(date)} "
            f"({date.strftime('%b')} {date.year}) rows 5-{4 + len(OFFSETS)}; "
            f"paste the next {len(OFFSETS)} lines into row 5 ---",
            file=sys.stderr,
        )
        sys.stdout.write("\n".join(day["labels"]) + "\n")


def print_summary(days):
    print("# summary (each filled 15-minute slot is 0.25 h)", file=sys.stderr)
    for day in days:
        date = day["date"]
        print(f"# {date.isoformat()} column {column_letter(date)}", file=sys.stderr)
        if day["first"] is None:
            print("#   first activity: none", file=sys.stderr)
            print("#   last activity: none", file=sys.stderr)
            continue
        print(f"#   first activity: {clock(day['first'])}", file=sys.stderr)
        print(f"#   last activity: {clock(day['last'])}", file=sys.stderr)
        ranked = sorted(day["hours"].items(), key=lambda item: (-item[1], item[0]))
        for label, hours in ranked:
            print(f"#   {label}: {hours:.2f} h", file=sys.stderr)


def parse_date(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise SystemExit(f"use YYYY-MM-DD, not {text!r}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Turn browser history into 15-minute sheet rows.")
    parser.add_argument("history", help="CSV, JSON, or pasted text export")
    parser.add_argument("--start", required=True, help="first sheet column, YYYY-MM-DD, Vancouver")
    parser.add_argument("--end", required=True, help="last sheet column, YYYY-MM-DD, Vancouver")
    parser.add_argument("--rules", default=str(Path(__file__).with_name("categories.json")))
    parser.add_argument("--summary", action="store_true", help="also print hours and first/last activity")
    args = parser.parse_args(argv)

    start, end = parse_date(args.start), parse_date(args.end)
    if end < start:
        raise SystemExit("--end is before --start")

    visits = load_visits(args.history)
    if not visits:
        raise SystemExit(f"no visits recognized in {args.history}")
    rules = load_rules(args.rules)
    days = build_days(visits, rules, start, end)
    in_range = sum(
        1
        for when, _title, _url in visits
        if start <= sheet_day_and_offset(when)[0] <= end
    )
    print(
        f"# {len(visits)} visits in file, {in_range} in columns {start.isoformat()} to {end.isoformat()}",
        file=sys.stderr,
    )
    print_blocks(days)
    if args.summary:
        print_summary(days)


if __name__ == "__main__":
    main()
