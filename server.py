#!/usr/bin/env python3
"""TAW squad statistics live dashboard.

Runs a small local HTTP server, fetches public TAW pages and calculates the
three views (current streak, best streak, current activity year) from sortie logs.
Uses only Python's standard library.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import html
import json
import math
import os
import re
import threading
import time
import traceback
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse, parse_qs
from urllib.request import Request, urlopen
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / ".taw_cache"
CACHE_DIR.mkdir(exist_ok=True)
INDEX_FILE = ROOT / "index.html"
BASE = "https://tacticalairwar.com/"
DEFAULT_SQUAD_URL = BASE + "squad_stats.php?name=72%20Air%20Group"
HOME_URL = BASE
CACHE_TTL_SECONDS = 60 * 60 * 2
MAX_WORKERS = 6
USER_AGENT = "TAW-Pilot-Streak-Dashboard/1.0 (public statistics viewer)"

STATE_LOCK = threading.RLock()
STATE: dict[str, Any] = {
    "running": False,
    "phase": "idle",
    "message": "Ожидание загрузки",
    "processed": 0,
    "total": 0,
    "profileProcessed": 0,
    "profileTotal": 0,
    "campaignYear": None,
    "mapName": None,
    "updatedAt": None,
    "error": None,
    "warnings": [],
    "pilots": [],
    "squadName": "72 Air Group",
    "squadUrl": DEFAULT_SQUAD_URL,
}

DATE_RE = re.compile(r"(?<!\d)(\d{1,2}\.\d{1,2}\.\d{4})(?:\s+(\d{1,2}:\d{2}:\d{2}))?")
SORTIE_PATH_RE = re.compile(r"pilot_sortie\.php", re.I)
PILOT_PATH_RE = re.compile(r"pilot\.php\?name=", re.I)

STATUS_ALIASES = {
    "landed": "LANDED",
    "killed": "KILLED",
    "ditched": "DITCHED",
    "disco": "DISCO",
    "bailed-out": "BAILED OUT",
    "bailed_out": "BAILED OUT",
    "crashed": "CRASHED",
    "captured": "CAPTURED",
    "in-flight": "IN FLIGHT",
    "in_flight": "IN FLIGHT",
}
BREAK_STATUSES = {"KILLED", "CAPTURED", "DISCO"}

# Ground-kill values on TAW are weighted: a ship contributes 10 GK, while the
# other classified target types contribute 1 each. Unknown targets are filled
# from the official Ground Kills total on the sortie page.
GROUND_CATEGORY_KEYS = (
    "heavyTank", "mediumTank", "lightTank", "aa", "groundedPlanes",
    "trains", "artillery", "vehicles", "ships",
)
GROUND_KILL_WEIGHTS = {key: (10 if key == "ships" else 1) for key in GROUND_CATEGORY_KEYS}

# These lists are taken from the user's existing program. The scoring formula
# intentionally preserves its current values, including the medium-plane
# coefficient being added twice (1 + 1.5) in the source's bally expression.
def load_list(filename: str) -> list[str]:
    path = ROOT / "lists" / filename
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip() and not line.strip().startswith("//")]

LIGHT_PLANES = load_list("light_plane.txt")
MIDDLE_PLANES = load_list("middle_plane.txt")
HEAVY_PLANES = load_list("heavy_plane.txt")
LIGHT_TANKS = load_list("light_t.txt")
MIDDLE_TANKS = load_list("middle_t.txt")
HEAVY_TANKS = load_list("heavy_t.txt")


def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


class Cell:
    def __init__(self, tag: str = "td"):
        self.tag = tag
        self.parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self.images: list[dict[str, str]] = []

    @property
    def text(self) -> str:
        return normalize(" ".join(self.parts))

    @property
    def label(self) -> str:
        if self.text:
            return self.text
        for image in self.images:
            for key in ("title", "alt"):
                if image.get(key):
                    return normalize(image[key]).replace("Image: ", "")
        return ""


class TableParser(HTMLParser):
    """Small tolerant table parser; preserves anchor URLs and status icon URLs."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[Cell]]] = []
        self._table: list[list[Cell]] | None = None
        self._row: list[Cell] | None = None
        self._cell: Cell | None = None
        self._link: dict[str, Any] | None = None
        self.headings: list[str] = []
        self._heading_depth = 0
        self._heading_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        a = dict(attrs)
        t = tag.lower()
        if t in ("h1", "h2", "h3", "h4"):
            self._heading_depth += 1
            self._heading_parts = []
        elif t == "table":
            if self._table is None:
                self._table = []
        elif t == "tr":
            self._row = []
        elif t in ("td", "th") and self._row is not None:
            self._cell = Cell(t)
        elif t == "a" and self._cell is not None:
            self._link = {"href": a.get("href", ""), "parts": []}
        elif t == "img" and self._cell is not None:
            self._cell.images.append({"src": a.get("src", ""), "alt": a.get("alt", ""), "title": a.get("title", "")})
        elif t == "br":
            if self._cell is not None:
                self._cell.parts.append(" ")
            if self._heading_depth:
                self._heading_parts.append(" ")

    def handle_data(self, data: str):
        if self._cell is not None:
            self._cell.parts.append(data)
        if self._link is not None:
            self._link["parts"].append(data)
        if self._heading_depth:
            self._heading_parts.append(data)

    def handle_endtag(self, tag: str):
        t = tag.lower()
        if t in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(self._cell)
            self._cell = None
        elif t == "a" and self._link is not None:
            if self._cell is not None:
                self._cell.links.append({"href": html.unescape(self._link["href"]), "text": normalize(" ".join(self._link["parts"]))})
            self._link = None
        elif t == "tr" and self._row is not None:
            if self._row and self._table is not None:
                self._table.append(self._row)
            self._row = None
        elif t == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None
        elif t in ("h1", "h2", "h3", "h4") and self._heading_depth:
            value = normalize(" ".join(self._heading_parts))
            if value:
                self.headings.append(value)
            self._heading_depth = 0
            self._heading_parts = []


def parse_tables(text: str) -> TableParser:
    parser = TableParser()
    parser.feed(text)
    parser.close()
    # In unusual markup table tags may be missing at the end.
    if parser._table:
        parser.tables.append(parser._table)
        parser._table = None
    return parser


def fetch_html(url: str, force: bool = False) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_path = CACHE_DIR / f"{digest}.json"
    if not force and cache_path.exists():
        try:
            saved = json.loads(cache_path.read_text(encoding="utf-8"))
            if time.time() - saved.get("saved_at", 0) < CACHE_TTL_SECONDS:
                return saved["html"]
        except Exception:
            pass
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.8"})
            with urlopen(req, timeout=25) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                body = raw.decode(charset, errors="replace")
                if len(body) < 300:
                    raise RuntimeError(f"TAW вернул слишком короткую страницу: {url}")
                cache_path.write_text(json.dumps({"saved_at": time.time(), "html": body}, ensure_ascii=False), encoding="utf-8")
                return body
        except (HTTPError, URLError, TimeoutError, OSError, RuntimeError) as exc:
            last_error = exc
            time.sleep(0.35 * (attempt + 1))
    raise RuntimeError(f"Не удалось загрузить TAW: {url} ({last_error})")


def number_from_cell(cell: Cell | None) -> int:
    if cell is None:
        return 0
    m = re.search(r"-?\d+", cell.text.replace(",", ""))
    return int(m.group()) if m else 0


def parse_date(text: str) -> datetime | None:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    try:
        date_part = m.group(1)
        time_part = m.group(2) or "00:00:00"
        return datetime.strptime(f"{date_part} {time_part}", "%d.%m.%Y %H:%M:%S")
    except ValueError:
        return None


def cell_text(row: list[Cell], index: int) -> str:
    return row[index].text if index < len(row) else ""


def all_row_links(row: list[Cell]) -> list[dict[str, str]]:
    return [link for cell in row for link in cell.links if link.get("href")]


def parse_squad_page(body: str) -> list[dict[str, Any]]:
    parsed = parse_tables(body)
    member_table: list[list[Cell]] | None = None
    header_row: list[Cell] | None = None
    for table in parsed.tables:
        for idx, row in enumerate(table):
            labels = " ".join(c.label for c in row).lower()
            if "pilot name" in labels:
                member_table = table
                header_row = row
                break
        if member_table:
            break
    if not member_table:
        raise RuntimeError("Не нашёл таблицу состава эскадрильи в ответе TAW. Возможно, сайт изменил разметку.")

    headers = [c.label.lower() for c in (header_row or [])]
    members: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in member_table:
        links = all_row_links(row)
        pilot_link = next((x for x in links if PILOT_PATH_RE.search(x["href"])), None)
        if not pilot_link:
            continue
        name = normalize(pilot_link.get("text") or cell_text(row, 0))
        if not name or name.lower() == "pilot name" or name in seen:
            continue
        seen.add(name)
        parsed_link = urlparse(urljoin(BASE, pilot_link["href"]))
        query_name = parse_qs(parsed_link.query).get("name", [name])[0]
        # On this page the first two cells are the linked nickname and its insignia/avatar.
        # Column indexes are stable on TAW; header labels are preserved for future variations.
        def col_value(pos: int, *needles: str) -> int:
            for idx, head in enumerate(headers):
                if idx < len(row) and any(needle in head for needle in needles):
                    return number_from_cell(row[idx])
            return number_from_cell(row[pos]) if pos < len(row) else 0
        member = {
            "name": name,
            "profileUrl": urljoin(BASE, pilot_link["href"]),
            "queryName": query_name,
            "official": {
                "airKills": col_value(2, "air kills"),
                "groundKills": col_value(3, "ground kills"),
                "airStreak": col_value(4, "air kill streak"),
                "groundStreak": col_value(5, "ground kill streak"),
                "deaths": col_value(6, "deaths"),
                "captures": col_value(7, "captures"),
                "disconnects": col_value(8, "disconnects"),
                "sorties": col_value(9, "sorties"),
                "flightTime": cell_text(row, 10),
                "experience": col_value(11, "experience"),
            },
        }
        members.append(member)
    if not members:
        raise RuntimeError("Страница TAW открылась, но в ней не найдено ни одного пилота.")
    return members


def status_from_row(row: list[Cell]) -> str:
    for cell in row:
        for img in cell.images:
            src = (img.get("src") or "").lower()
            for token, label in STATUS_ALIASES.items():
                if token in src:
                    return label
            alt_title = (img.get("alt", "") + " " + img.get("title", "")).upper()
            for label in STATUS_ALIASES.values():
                if label in alt_title:
                    return label
        txt = cell.text.upper()
        for label in STATUS_ALIASES.values():
            if txt == label:
                return label
    return "UNKNOWN"


def parse_profile(body: str, pilot_name: str) -> list[dict[str, Any]]:
    parsed = parse_tables(body)
    flights: dict[str, dict[str, Any]] = {}
    for table in parsed.tables:
        for row in table:
            links = all_row_links(row)
            sortie_link = next((x for x in links if SORTIE_PATH_RE.search(x["href"])), None)
            if not sortie_link:
                continue
            row_date = None
            # Usually the date is in the linked first column; search all cells to tolerate column layout changes.
            for cell in row:
                row_date = parse_date(cell.text)
                if row_date:
                    break
            if not row_date:
                continue
            url = urljoin(BASE, sortie_link["href"])
            # De-duplicate: profile pages repeat the newest sortie in a short current-life table and the full log.
            if url in flights:
                continue
            mission = cell_text(row, 1)
            flight = {
                "url": url,
                "date": row_date.isoformat(timespec="seconds"),
                "mission": mission,
                "status": status_from_row(row),
                "flightTime": cell_text(row, 3),
                "airKillsOfficial": number_from_cell(row[4] if len(row) > 4 else None),
                "groundKillsOfficial": number_from_cell(row[5] if len(row) > 5 else None),
                "aircraft": cell_text(row, 6),
                "remarks": cell_text(row, 7),
                "profileRowText": " ".join(cell.text for cell in row),
                "pilot": pilot_name,
            }
            flights[url] = flight
    return sorted(flights.values(), key=lambda x: x["date"], reverse=True)


def parse_event_log(body: str) -> list[dict[str, str]]:
    parsed = parse_tables(body)
    events: list[dict[str, str]] = []
    for table in parsed.tables:
        header_idx = None
        columns: dict[str, int] = {}
        for i, row in enumerate(table):
            labels = [c.label.lower() for c in row]
            if "event" in labels and any("date" in v for v in labels):
                header_idx = i
                for j, label in enumerate(labels):
                    if "date" in label: columns["date"] = j
                    elif label == "event" or "event" in label: columns["event"] = j
                    elif "object" in label: columns["object"] = j
                    elif label == "type" or "type" in label: columns["type"] = j
                    elif "enemy" in label: columns["enemy"] = j
                    elif "damage" in label: columns["damage"] = j
                break
        if header_idx is None:
            continue
        for row in table[header_idx + 1:]:
            d = cell_text(row, columns.get("date", 0))
            ev = cell_text(row, columns.get("event", 1)).upper()
            if not parse_date(d) or not ev:
                continue
            events.append({
                "date": normalize(d),
                "event": ev,
                "damage": cell_text(row, columns.get("damage", -1)) if "damage" in columns else "",
                "object": cell_text(row, columns.get("object", -1)) if "object" in columns else "",
                "type": cell_text(row, columns.get("type", -1)) if "type" in columns else "",
                "enemy": cell_text(row, columns.get("enemy", -1)) if "enemy" in columns else "",
            })
        if events:
            return events
    return events


def has_any(value: str, choices: list[str]) -> bool:
    value_low = value.casefold()
    return any(choice.casefold() in value_low for choice in choices)


def is_air_kill(event: dict[str, str]) -> bool:
    # TAW's event column uses the exact label SHOT DOWN for a kill by this pilot.
    # WAS SHOT DOWN is the opposite event and must never count as a victory.
    return normalize(event.get("event", "")).upper() == "SHOT DOWN"


def classify_plane(plane: str) -> list[str]:
    cats: list[str] = []
    if has_any(plane, LIGHT_PLANES): cats.append("fighter")
    if has_any(plane, MIDDLE_PLANES): cats.append("attack")
    if has_any(plane, HEAVY_PLANES): cats.append("bomber")
    return cats


def classify_ground(event: dict[str, str]) -> str | None:
    """Return a recognized legacy target category, or None for unclassified targets.

    Match the old application's markers against the object name and type.
    AA and AT are case-sensitive substrings to mirror the original C++ Contains()
    checks; unrelated targets are then reconciled against TAW's official total.
    """
    obj = normalize(event.get("object", ""))
    typ = normalize(event.get("type", ""))
    combined = f"{obj} {typ}"
    upper = combined.upper()

    if any(x in upper for x in ("BOTGUNNER", "TURRET", "GUNNER WAS DESTROYED")):
        return None
    if has_any(combined, HEAVY_TANKS):
        return "heavyTank"
    if has_any(combined, MIDDLE_TANKS):
        return "mediumTank"
    if has_any(combined, LIGHT_TANKS):
        return "lightTank"
    if "STATIC_PLANE" in upper or typ.casefold() == "plane":
        return "groundedPlanes"
    # Mirror the legacy Contains("AA") / Contains("AT") checks against the
    # original text, preserving their case-sensitive behavior.
    if "AA" in combined:
        return "aa"
    if "WAGON" in upper or "TRAIN" in upper:
        return "trains"
    if "ARTILLERY" in upper or "ARTY" in upper or "AT" in combined:
        return "artillery"
    if "TRUCK" in upper or "APC" in upper:
        return "vehicles"
    if "SHIP" in upper:
        return "ships"
    return None


def parse_ground_kills_official(body: str) -> int | None:
    """Read the authoritative Ground Kills value shown near the top of a sortie page."""
    plain_text = normalize(re.sub(r"<[^>]+>", " ", html.unescape(body)))
    match = re.search(r"\bGround\s+Kills?\s*(?:[:|]\s*)?(\d+)\b", plain_text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def round_bally(value: float) -> int:
    return int(math.floor(value + 0.5)) if value >= 0 else int(math.ceil(value - 0.5))


def analyze_flight(
    flight: dict[str, Any],
    events: list[dict[str, str]],
    official_ground_kills: int | None = None,
) -> dict[str, Any]:
    counts = {k: 0 for k in ["fighter", "attack", "bomber", "heavyTank", "mediumTank", "lightTank", "aa", "groundedPlanes", "trains", "artillery", "vehicles", "ships", "otherGround"]}
    # TAW's official sortie total is authoritative. If it is explicitly zero,
    # do not surface DESTROYED events as ground targets in the dashboard.
    if official_ground_kills is None:
        official_ground_kills = int(flight.get("groundKillsOfficial", 0) or 0)
    official_ground_kills = max(0, int(official_ground_kills or 0))
    victories = []
    ground_events = []
    wounds = 0
    for event in events:
        ev_name = event.get("event", "").upper()
        if is_air_kill(event):
            plane = normalize(event.get("object", "Unknown aircraft")) or "Unknown aircraft"
            for category in classify_plane(plane):
                counts[category] += 1
            victories.append({
                "date": (parse_date(event.get("date", "")) or datetime.fromisoformat(flight["date"])).strftime("%d.%m.%Y"),
                "time": (parse_date(event.get("date", "")) or datetime.fromisoformat(flight["date"])).strftime("%H:%M:%S"),
                "plane": plane,
                "pilot": normalize(event.get("enemy", "")) or "Не указан в логе",
                "mission": flight.get("mission", ""),
                "sortieUrl": flight["url"],
            })
        if ev_name == "DESTROYED" and official_ground_kills > 0:
            cat = classify_ground(event)
            if cat:
                # Category rows display object counts (one ship = one ship).
                # TAW Ground Kills and bally scoring apply the separate GK weight.
                counts[cat] += 1
                ground_events.append({
                    "object": normalize(event.get("object", "Unknown target")),
                    "type": normalize(event.get("type", "")),
                    "category": cat,
                })
        if "PILOT WAS DAMAGED" in ev_name and any(t in (event.get("type", "") + " " + event.get("object", "")).upper() for t in ("PLANE", "AA", "ARTY", "ARTILLERY", " AT ")):
            wounds = 1

    # Prefer the sortie page's official Ground Kills number, then fall back to
    # the same value parsed from the pilot's sortie list.
    classified_ground = sum(counts[key] * GROUND_KILL_WEIGHTS[key] for key in GROUND_CATEGORY_KEYS)
    counts["otherGround"] = max(0, official_ground_kills - classified_ground)
    ground_mismatch = classified_ground > official_ground_kills

    # Preserve the existing program's current scoring expression exactly:
    # lp + mp*1 + mp*1.5 + hp*2 + ground coefficients.
    score = (
        counts["fighter"]
        + counts["attack"] * 1.0
        + counts["attack"] * 1.5
        + counts["bomber"] * 2.0
        + counts["heavyTank"] * 0.7
        + counts["mediumTank"] * 0.5
        + counts["lightTank"] * 0.3
        + counts["aa"] * 0.1
        + counts["groundedPlanes"] * 0.08
        + counts["trains"] * 0.07
        + counts["artillery"] * 0.06
        + counts["vehicles"] * 0.05
        + counts["ships"] * GROUND_KILL_WEIGHTS["ships"] * 1.0
        + counts["otherGround"] * 0.04
    )
    # The total displayed for the sortie is authoritative TAW Ground Kills.
    # When classified targets do not exceed it, "otherGround" makes the breakdown
    # reconcile exactly. If classifications exceed the official figure, keep the
    # official total and surface a warning rather than silently inflating the total.
    ground_total = official_ground_kills
    return {
        "flight": flight,
        "events": events,
        "scoreRaw": score,
        "score": round_bally(score),
        "counts": counts,
        "planes": len(victories),
        "ground": ground_total,
        "groundOfficial": official_ground_kills,
        "groundClassified": classified_ground,
        "groundMismatch": ground_mismatch,
        "wounds": wounds,
        "victories": victories,
        "groundEvents": ground_events,
        "breaker": is_breaker(flight, events),
        "pilotExit": is_pilot_exit(flight, events),
    }


def is_pilot_exit(flight: dict[str, Any], events: list[dict[str, str]]) -> bool:
    """PILOT EXIT invalidates a sortie's statistics but does not break a streak."""
    candidates = [
        flight.get("status", ""),
        flight.get("remarks", ""),
        flight.get("profileRowText", ""),
        flight.get("mission", ""),
        *(value for event in events for value in event.values()),
    ]
    return any(
        re.search(r"\bPILOT[\s_-]+EXIT\b", normalize(value), re.IGNORECASE)
        for value in candidates
        if value
    )


def is_breaker(flight: dict[str, Any], events: list[dict[str, str]]) -> bool:
    status = normalize(flight.get("status", "")).upper()
    if status in BREAK_STATUSES:
        return True

    # Do not rely only on the profile-page status icon: it can be absent or
    # misread from the HTML. The sortie's event log is the strongest evidence
    # that a death, capture, or disconnect ended the flight/streak.
    for event in events:
        event_name = normalize(event.get("event", "")).upper()
        if event_name in BREAK_STATUSES:
            return True
        if re.search(r"\b(?:KILLED|CAPTURED|DISCO)\b", event_name):
            return True

    # BAILED OUT by itself preserves a streak; CAPTURED above breaks it.
    # LANDED also preserves the streak.
    return False


def empty_mode() -> dict[str, Any]:
    return {"score": 0, "scoreRaw": 0.0, "sorties": 0, "planes": 0, "ground": 0, "wounds": 0,
            "fighter": 0, "attack": 0, "bomber": 0,
            "typeCounts": {k: 0 for k in ["heavyTank", "mediumTank", "lightTank", "aa", "groundedPlanes", "trains", "artillery", "vehicles", "ships", "otherGround"]},
            "airVictories": []}


def summarize_flights(analyzed: list[dict[str, Any]]) -> dict[str, Any]:
    summary = empty_mode()
    for item in analyzed:
        summary["scoreRaw"] += item["scoreRaw"]
        summary["sorties"] += 1
        summary["planes"] += item["planes"]
        summary["ground"] += item["ground"]
        summary["wounds"] += item["wounds"]
        summary["fighter"] += item["counts"]["fighter"]
        summary["attack"] += item["counts"]["attack"]
        summary["bomber"] += item["counts"]["bomber"]
        for key in summary["typeCounts"]:
            summary["typeCounts"][key] += item["counts"][key]
        summary["airVictories"].extend(item["victories"])
    summary["score"] = round_bally(summary["scoreRaw"])
    summary["airVictories"].sort(key=lambda v: (v.get("date", ""), v.get("time", "")), reverse=True)
    return summary


def calculate_modes(flights: list[dict[str, Any]]) -> dict[str, Any]:
    # Flights arrive newest -> oldest, exactly like TAW's public pilot page.
    all_analyzed = [x.get("analysis") for x in flights if x.get("analysis") is not None]
    all_analyzed = sorted(all_analyzed, key=lambda x: x["flight"].get("date", ""), reverse=True)
    # Breaker sorties and PILOT EXIT sorties contribute no statistics.
    # PILOT EXIT is not a breaker, so it must not split a streak segment.
    counted_analyzed = [
        item for item in all_analyzed
        if not item["breaker"] and not item.get("pilotExit", False)
    ]
    campaign = summarize_flights(counted_analyzed)
    segments: list[list[dict[str, Any]]] = []
    segment: list[dict[str, Any]] = []
    first_segment: list[dict[str, Any]] = []
    saw_first_break = False
    for item in all_analyzed:
        if item["breaker"]:
            if segment:
                segments.append(segment)
            if not saw_first_break:
                first_segment = list(segment)
                saw_first_break = True
            segment = []
        elif not item.get("pilotExit", False):
            # Skip the sortie's scores without breaking the streak continuity.
            segment.append(item)
    if segment:
        segments.append(segment)
    if not saw_first_break:
        first_segment = list(segment)
    current = summarize_flights(first_segment)
    best_segment = max(segments, key=lambda seg: sum(x["scoreRaw"] for x in seg), default=[])
    best = summarize_flights(best_segment)
    return {"current": current, "best": best, "campaign": campaign}


def scrape_current_map(html_body: str) -> str | None:
    parsed = parse_tables(html_body)
    pattern = re.compile(r"\b[A-Za-z][A-Za-z0-9 .'-]+\s+#\d+\b")
    # The map heading appears among H1-H4 on the home page. Avoid other page headings.
    for heading in parsed.headings:
        if pattern.search(heading):
            return normalize(pattern.search(heading).group())
    # fallback only if a heading has been flattened or is not semantically marked
    text = re.sub(r"<[^>]+>", " ", html_body)
    m = re.search(r"(?:\n|\s)([A-Za-z][A-Za-z0-9 .'-]+\s+#\d+)(?:\s|\n)", html.unescape(text))
    return normalize(m.group(1)) if m else None


def update_state(**kwargs):
    with STATE_LOCK:
        STATE.update(kwargs)


def build_snapshot(force: bool = False, squad_url: str | None = None):
    warnings: list[str] = []
    squad_url = validate_squad_url(squad_url or public_state().get("squadUrl") or DEFAULT_SQUAD_URL)
    try:
        update_state(running=True, phase="squad", message="Загружаю состав эскадрильи TAW…", processed=0, total=0,
                     profileProcessed=0, profileTotal=0, error=None, warnings=[], pilots=[], campaignYear=None, mapName=None)
        try:
            home_body = fetch_html(HOME_URL, force=force)
            map_name = scrape_current_map(home_body)
        except Exception as exc:
            # Squad data should still load when the homepage/current-map section is temporarily unavailable.
            map_name = None
            warnings.append(f"Не удалось определить текущую карту TAW: {exc}")
        squad_body = fetch_html(squad_url, force=force)
        members = parse_squad_page(squad_body)
        squad_name = parse_squad_name(squad_body, squad_url)
        update_state(squadName=squad_name, squadUrl=squad_url)
        update_state(phase="profiles", message="Читаю страницы пилотов и нахожу последний год вылетов…",
                     profileTotal=len(members), profileProcessed=0, mapName=map_name, squadName=squad_name, squadUrl=squad_url, pilots=[{
                         "name": m["name"], "official": m["official"], "profileUrl": m["profileUrl"],
                         "current": None, "best": None, "campaign": None, "loading": True
                     } for m in members])

        profile_results: dict[str, list[dict[str, Any]]] = {}
        profile_errors: dict[str, str] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futs = {pool.submit(fetch_html, member["profileUrl"], force): member for member in members}
            for fut in concurrent.futures.as_completed(futs):
                member = futs[fut]
                try:
                    profile_html = fut.result()
                    profile_results[member["name"]] = parse_profile(profile_html, member["name"])
                    if not profile_results[member["name"]]:
                        warnings.append(f"{member['name']}: не найдены ссылки на вылеты")
                except Exception as exc:
                    profile_results[member["name"]] = []
                    profile_errors[member["name"]] = str(exc)
                    warnings.append(f"{member['name']}: ошибка профиля")
                with STATE_LOCK:
                    STATE["profileProcessed"] += 1
        years = []
        for flights in profile_results.values():
            for flight in flights:
                dt = datetime.fromisoformat(flight["date"])
                years.append(dt.year)
        if not years:
            raise RuntimeError("Не удалось определить даты вылетов в профилях TAW; расчёт кампании невозможен.")
        season_year = max(years)
        filtered_profiles: dict[str, list[dict[str, Any]]] = {}
        for member in members:
            name = member["name"]
            filtered_profiles[name] = [f for f in profile_results.get(name, []) if datetime.fromisoformat(f["date"]).year == season_year]
        total_sorties = sum(len(v) for v in filtered_profiles.values())
        update_state(phase="sorties", message=f"Загружаю журналы вылетов за {season_year} год…", campaignYear=season_year,
                     processed=0, total=total_sorties, profileTotal=len(members), profileProcessed=len(members), warnings=warnings)

        analyses_by_pilot: dict[str, list[dict[str, Any]]] = {m["name"]: [] for m in members}
        expected_by_pilot = {m["name"]: len(filtered_profiles.get(m["name"], [])) for m in members}
        completed_by_pilot = {m["name"]: 0 for m in members}
        member_by_name = {m["name"]: m for m in members}
        task_list: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for member in members:
            for flight in filtered_profiles.get(member["name"], []):
                task_list.append((member, flight))

        # Pilots with no sorties during the latest detected year can be shown immediately.
        for member in members:
            if expected_by_pilot[member["name"]] == 0:
                empty = {"name": member["name"], "official": member["official"], "profileUrl": member["profileUrl"],
                         "loading": False, "dataWarning": profile_errors.get(member["name"]), **calculate_modes([])}
                with STATE_LOCK:
                    for idx, p in enumerate(STATE["pilots"]):
                        if p["name"] == member["name"]:
                            STATE["pilots"][idx] = empty
                            break

        def load_sortie(task):
            member, flight = task
            page = fetch_html(flight["url"], force=force)
            events = parse_event_log(page)
            if not events:
                raise RuntimeError("Таблица событий не найдена")
            official_ground_kills = parse_ground_kills_official(page)
            analysis = analyze_flight(flight, events, official_ground_kills)
            if analysis["groundMismatch"]:
                warnings.append(
                    f"{member['name']}, {flight.get('date', '')}: сумма классифицированных "
                    f"наземных целей ({analysis['groundClassified']}) выше Ground Kills TAW "
                    f"({analysis['groundOfficial']}); проверьте классификацию."
                )
            return member["name"], analysis

        def publish_pilot(name: str):
            member = member_by_name[name]
            modes = calculate_modes(analyses_by_pilot.get(name, []))
            entry = {"name": name, "official": member["official"], "profileUrl": member["profileUrl"],
                     "loading": False, "dataWarning": profile_errors.get(name), **modes}
            with STATE_LOCK:
                for idx, p in enumerate(STATE["pilots"]):
                    if p["name"] == name:
                        STATE["pilots"][idx] = entry
                        break

        failures = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futs = {pool.submit(load_sortie, task): task for task in task_list}
            for fut in concurrent.futures.as_completed(futs):
                member, flight = futs[fut]
                name = member["name"]
                try:
                    pilot_name, analysis = fut.result()
                    analyses_by_pilot[pilot_name].append({"analysis": analysis, "flight": analysis["flight"]})
                except Exception:
                    failures += 1
                    # A page failure should not silently contribute incorrect zero kills.
                    warnings.append(f"{name}, {flight.get('date','')}: не прочитан журнал вылета")
                completed_by_pilot[name] += 1
                if completed_by_pilot[name] >= expected_by_pilot[name]:
                    publish_pilot(name)
                with STATE_LOCK:
                    STATE["processed"] += 1
                    done_count = STATE["processed"]
                    if done_count % 5 == 0 or done_count == STATE["total"]:
                        STATE["message"] = f"Обработано журналов вылетов: {done_count} из {STATE['total']}…"

        results = []
        for member in members:
            name = member["name"]
            existing = next((p for p in public_state()["pilots"] if p["name"] == name and p.get("loading") is False and p.get("current") is not None), None)
            if existing:
                results.append(existing)
            else:
                results.append({"name": name, "official": member["official"], "profileUrl": member["profileUrl"],
                                "loading": False, "dataWarning": profile_errors.get(name), **calculate_modes(analyses_by_pilot.get(name, []))})
        results.sort(key=lambda p: (-(p.get("campaign") or {}).get("score", 0), p["name"].casefold()))
        if failures:
            warnings.append(f"Не удалось прочитать {failures} журнал(ов). В расчётах таких вылетов данные могут быть неполными.")
        update_state(running=False, phase="complete", message="Данные TAW загружены", pilots=results,
                     campaignYear=season_year, mapName=map_name, updatedAt=datetime.now().astimezone().isoformat(timespec="seconds"),
                     warnings=warnings[:30], error=None)
    except Exception as exc:
        update_state(running=False, phase="error", message="Не удалось загрузить данные TAW", error=str(exc),
                     warnings=warnings, updatedAt=datetime.now().astimezone().isoformat(timespec="seconds"))


def validate_squad_url(value: str) -> str:
    """Only allow public TAW squad-statistics pages, not arbitrary URLs."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("Вставь ссылку на страницу эскадрильи TAW.")
    parsed = urlparse(raw)
    if parsed.scheme not in ("https", "http") or parsed.hostname not in ("tacticalairwar.com", "www.tacticalairwar.com"):
        raise ValueError("Принимается только ссылка с сайта tacticalairwar.com.")
    if parsed.path.rstrip("/") != "/squad_stats.php":
        raise ValueError("Это не страница эскадрильи. Нужна ссылка вида tacticalairwar.com/squad_stats.php?name=...")
    if not parse_qs(parsed.query).get("name"):
        raise ValueError("В ссылке не найден параметр name — скопируй полный адрес страницы сквада.")
    # Normalize to HTTPS and strip fragments to avoid duplicate cache entries.
    return "https://tacticalairwar.com/squad_stats.php?" + parsed.query


def parse_squad_name(body: str, url: str) -> str:
    parsed = parse_tables(body)
    # In the live page, the first meaningful heading is the squadron name.
    for heading in parsed.headings:
        value = normalize(heading).lstrip("# ")
        if value and value.lower() not in {"tactical air war", "combat record", "squadron members", "squadron info"}:
            return value
    name = parse_qs(urlparse(url).query).get("name", ["Эскадрилья TAW"])[0]
    return normalize(name.replace("+", " ")) or "Эскадрилья TAW"


def start_job(force: bool = False, squad_url: str | None = None):
    with STATE_LOCK:
        if STATE["running"]:
            return False
        try:
            normalized_url = validate_squad_url(squad_url or STATE.get("squadUrl") or DEFAULT_SQUAD_URL)
        except ValueError as exc:
            STATE.update({"error": str(exc), "phase": "error", "message": "Проверь ссылку на эскадрилью"})
            return False
        STATE.update({"running": True, "phase": "starting", "message": "Запускаю сбор данных…", "error": None,
                      "warnings": [], "processed": 0, "total": 0, "profileProcessed": 0, "profileTotal": 0,
                      "squadUrl": normalized_url, "pilots": []})
    threading.Thread(target=build_snapshot, args=(force, normalized_url), daemon=True, name="taw-scraper").start()
    return True


def public_state() -> dict[str, Any]:
    with STATE_LOCK:
        return json.loads(json.dumps(STATE, ensure_ascii=False))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        print("[TAW] " + (fmt % args))

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def respond_json(self, data: Any, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/data":
            return self.respond_json(public_state())
        if parsed.path == "/api/status":
            return self.respond_json(public_state())
        if parsed.path == "/api/refresh":
            params = parse_qs(parsed.query)
            force = params.get("force", ["1"])[0] != "0"
            started = start_job(force=force)
            return self.respond_json({**public_state(), "started": started}, 202 if started else 409)
        if parsed.path == "/api/health":
            state = public_state()
            return self.respond_json({"ok": True, "source": state.get("squadUrl"), "server": "local"})
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/start":
            return self.respond_json({"error": "Unknown endpoint"}, 404)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 20_000:
                return self.respond_json({"error": "Слишком большой запрос"}, 413)
            payload = json.loads(self.rfile.read(size).decode("utf-8") or "{}")
            squad_url = validate_squad_url(str(payload.get("squadUrl", "")))
            started = start_job(force=bool(payload.get("force", False)), squad_url=squad_url)
            state = public_state()
            state["started"] = started
            status = 202 if started else (409 if state.get("running") else 400)
            return self.respond_json(state, status)
        except (ValueError, json.JSONDecodeError) as exc:
            return self.respond_json({"error": str(exc)}, 400)


def main():
    # Render and similar hosts provide PORT and require binding to 0.0.0.0.
    # For local Windows use, keep the server private to this computer by default.
    is_hosted = bool(os.environ.get("PORT"))
    host = os.environ.get("HOST", "0.0.0.0" if is_hosted else "127.0.0.1")
    port = int(os.environ.get("PORT", os.environ.get("TAW_PORT", "8000")))
    server = ThreadingHTTPServer((host, port), Handler)
    print("\nTAW: веб-страница готова")
    if is_hosted:
        print(f"Сервер слушает порт {port} на {host} (режим хостинга)")
    else:
        print(f"Открой в браузере: http://{host}:{port}")
    print("Источник данных: публичные страницы tacticalairwar.com")
    print("Вставь URL страницы squad_stats.php в поле и нажми «Загрузить данные».")
    print("Для остановки нажми Ctrl+C. Первичная загрузка журналов может занять несколько минут.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
