#!/usr/bin/env python3
"""
PTA TSR Collector v2.4.2
========================

Single-file PTA Weekly Notice collector, extractor, normaliser, review workflow
and Power BI analytics exporter.

v2.4.2 changes:
- repair-first row extraction instead of reject-first validation;
- parser artefacts are counted separately and not sent to manual TSR review;
- multiple extraction strategies with strategy scoring;
- speed glyph normalisation, including 80km’h / 80kmh / 80 kph;
- decimal chainage parsing without requiring every number to have a km suffix;
- named-location TSR rows accepted where otherwise valid;
- cleaner manual review template plus generated .txt instructions;
- apply-review workflow for corrected rows;
- hard QA gates before analytics export.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import importlib.util
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote, unquote, urljoin, urlparse, urlsplit, urlunsplit

APP_NAME = "pta_tsr_collector"
APP_VERSION = "2.4.2"
PTA_BASE_URL = "https://www.pta.wa.gov.au"
DEFAULT_SEED_URL = f"{PTA_BASE_URL}/about-us/working-with-the-pta/safety-resources"
PTA_HOST_SUFFIX = "pta.wa.gov.au"
DNN_CONTENT_ENDPOINT = f"{PTA_BASE_URL}/API/DocumentViewer/ContentService/GetFolderContent"
DEFAULT_DNN_MODULE_ID = "3195"
DEFAULT_DNN_TAB_ID = "1198"
DEFAULT_DNN_FOLDER_IDS = [5160]

ROOT = Path.cwd()
DATA_DIR = ROOT / "pta_tsr_data"
PDF_DIR = DATA_DIR / "pdfs"
EXPORT_DIR = DATA_DIR / "exports"
ANALYTICS_DIR = DATA_DIR / "analytics"
LOG_DIR = DATA_DIR / "logs"
DIAGNOSTICS_DIR = DATA_DIR / "diagnostics"
REVIEW_DIR = DATA_DIR / "review"
DB_PATH = DATA_DIR / "pta_tsr.sqlite3"
LOG_PATH = LOG_DIR / "pta_tsr_collector.log"

REQUIRED_PACKAGES = {
    "requests": "requests",
    "bs4": "beautifulsoup4",
    "pdfplumber": "pdfplumber",
}

LINE_PREFIXES = {
    "JTM": "Joondalup Line",
    "MTM": "Midland Line",
    "FTM": "Fremantle Line",
    "ATM": "Armadale Line",
    "TTM": "Thornlie Line",
    "RTM": "Mandurah Line",
    "MDM": "Mandurah Line",
    "CTM": "City / Central Area",
    "CP": "City / Central Area",
}

LINE_ALIASES = [
    ("Joondalup Line", "JTM", ["leederville", "glendalough", "stirling", "warwick", "whitfords", "edgewater", "joondalup", "nowergup", "greenwood", "currambine", "butler", "alkimos", "eglinton"]),
    ("Fremantle Line", "FTM", ["fremantle", "robbs jetty", "shenton park", "subiaco", "showgrounds", "claremont", "cottesloe", "mosman park", "victoria street", "north fremantle"]),
    ("Midland Line", "MTM", ["midland", "bassendean", "success hill", "east guildford", "guildford", "bayswater", "maylands", "meltham", "ashfield", "woodbridge"]),
    ("Armadale Line", "ATM", ["armadale", "kelmscott", "gosnells", "maddington", "kenwick", "beckenham", "oats st", "cannington", "queens park", "carlisle", "sherwood"]),
    ("Mandurah Line", "RTM", ["mandurah", "rockingham", "warnbro", "kwinana", "wellard", "cockburn", "glen iris", "aubin grove", "murdoch", "bull creek", "canning bridge", "elizabeth quay"]),
    ("Thornlie Line", "TTM", ["thornlie"]),
]

BAD_REASON_RE = re.compile(
    r"^(?:TBA|TBC|TBD|N/A|INDEFINITE|PERMANENT TSR|STN NO\.?|UP MAIN|DOWN MAIN|UP DIRECTION|DOWN DIRECTION|DIRECTION|\d{2}-\d{2}-\d{3}|\d{1,2}/\d{1,2}/\d{2,4}|\d+(?:\.\d+)?\s*KM.*)$",
    re.I,
)
SPEED_RE = re.compile(r"\b(?P<speed>\d{1,3})\s*(?:k\s*m\s*[/’'`´]?\s*h|kmh|kph|km\s*hr|km/hr)\b", re.I)
STN_RE = re.compile(r"\b\d{2}-\d{2}-\d{3}\b")
CANCEL_TOKEN_RE = re.compile(r"\b(?:TBA|TBC|TBD|N/A|Indefinite|Permanent TSR|Cancelled)\b", re.I)
DATE_TOKEN_RE = re.compile(
    r"\b(?:\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\s+\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}|[A-Za-z]{3,9}\s+\d{1,2}(?:st|nd|rd|th)?\s+\d{2,4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})\b",
    re.I,
)
CHAINAGE_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,4})\s*(?:km)?\b", re.I)


class PipelineError(Exception):
    pass


class MissingPdfError(PipelineError):
    pass


@dataclass(frozen=True)
class DiscoveredPdf:
    url: str
    filename: str
    notice_date: Optional[str]


@dataclass(frozen=True)
class CandidateRow:
    cells: list[str]
    source_page: int
    source_row_number: int
    strategy: str
    raw_text: str


@dataclass(frozen=True)
class ExtractedRow:
    notice_date: str
    location: str
    line_section: str
    distance_km: str
    stn_no: str
    max_speed: str
    date_imposed_raw: str
    date_imposed_normalised: str
    reason_raw: str
    reason_group: str
    date_cancelled_raw: str
    date_cancelled_normalised: str
    line_key: str
    line_name: str
    location_direction: str
    affected_area: str
    location_from_km: Optional[float]
    location_to_km: Optional[float]
    source_page: int
    source_row_number: int
    extraction_strategy: str
    row_quality: str
    normalisation_notes: str
    raw_row_json: str


@dataclass(frozen=True)
class RejectedRow:
    notice_date: str
    source_page: int
    source_row_number: int
    reject_reason: str
    rejection_category: str
    raw_row_json: str
    suggested_action: str


@dataclass(frozen=True)
class StrategyResult:
    strategy: str
    rows: list[ExtractedRow]
    rejects: list[RejectedRow]
    artifact_count: int
    candidate_count: int

    @property
    def score(self) -> tuple[int, int, int, int]:
        return (len(self.rows), -len(self.rejects), -self.artifact_count, -self.candidate_count)


def ensure_dependencies(auto_install: bool = False) -> None:
    missing = [pkg for mod, pkg in REQUIRED_PACKAGES.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    if not auto_install:
        answer = input(f"Missing Python packages {missing}. Install now? [Y/n]: ").strip().lower()
        if answer not in {"", "y", "yes"}:
            raise PipelineError(f"Install dependencies manually: {sys.executable} -m pip install {' '.join(missing)}")
    subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])


def import_runtime_dependencies() -> None:
    global requests, BeautifulSoup, pdfplumber
    import requests  # type: ignore
    from bs4 import BeautifulSoup  # type: ignore
    import pdfplumber  # type: ignore


def setup_dirs_and_logging(verbose: bool = False) -> None:
    for path in (DATA_DIR, PDF_DIR, EXPORT_DIR, ANALYTICS_DIR, LOG_DIR, DIAGNOSTICS_DIR, REVIEW_DIR):
        path.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def clean(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    replacements = {
        "\u2013": "-", "\u2014": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
        "’": "'", "‘": "'", "`": "'", "´": "'", "\u00a0": " ", "\u200b": "",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip()


def norm_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9.]+", "", clean(value).upper())


def is_blank_cells(cells: list[str]) -> bool:
    return not any(clean(c) for c in cells)


def is_low_content_fragment(cells: list[str]) -> bool:
    nonblank = [clean(c) for c in cells if clean(c)]
    if not nonblank:
        return True
    if len(nonblank) <= 1 and not SPEED_RE.search(nonblank[0]):
        return True
    text = " ".join(nonblank)
    if re.fullmatch(r"(?:Up|Down)?\s*(?:Main|Direction)", text, flags=re.I):
        return True
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,4})?\s*km?\s*(?:to)?", text, flags=re.I):
        return True
    return False


def normalise_speed(value: str) -> str:
    text = clean(value).lower()
    match = SPEED_RE.search(text)
    if not match:
        return ""
    speed = int(match.group("speed"))
    if not (1 <= speed <= 160):
        return ""
    return f"{speed}km/h"


def extract_speed_from_cells(cells: list[str]) -> tuple[str, Optional[int]]:
    for idx, cell in enumerate(cells):
        speed = normalise_speed(cell)
        if speed:
            return speed, idx
    joined = " | ".join(cells)
    speed = normalise_speed(joined)
    return (speed, None) if speed else ("", None)


def parse_date(value: str, notice_year: Optional[int] = None) -> str:
    text = clean(value).strip(" -,.()")
    if not text or text.upper() in {"TBA", "TBC", "TBD", "N/A", "INDEFINITE", "PERMANENT TSR", "CANCELLED"}:
        return ""
    text = text.replace("Febuary", "February")
    text = re.sub(r"\bSept\b", "Sep", text, flags=re.I)
    text = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", text, flags=re.I)
    text = re.sub(r"([A-Za-z])(?=\d{4}\b)", r"\1 ", text)
    text = clean(text)
    if notice_year and re.match(r"^\d{1,2}\s+[A-Za-z]{3,9}$", text):
        text = f"{text} {notice_year}"
    if notice_year and re.match(r"^[A-Za-z]{3,9}\s+\d{1,2}$", text):
        text = f"{text} {notice_year}"
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y", "%d/%m/%Y", "%d-%m-%Y", "%d %B %y", "%d %b %y", "%d/%m/%y", "%d-%m-%y"):
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def notice_date_from_url(url: str) -> Optional[str]:
    stem = re.sub(r"\.pdf$", "", unquote(urlparse(url).path.rsplit("/", 1)[-1]), flags=re.I)
    for pat in (r"Week\s+Commencing\s+(.+)$", r"Week\s+Ending\s*(?:Fri\s*)?[- ]*(.+)$"):
        m = re.search(pat, stem, flags=re.I)
        if m:
            parsed = parse_date(m.group(1))
            if parsed:
                return parsed
    return None


def safe_filename(url: str) -> str:
    raw = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "weekly_notice.pdf"
    raw = re.sub(r"[<>:\"/\\|?*]+", "_", raw)
    if not raw.lower().endswith(".pdf"):
        raw += ".pdf"
    return f"{raw[:-4]}__{hashlib.sha1(url.encode('utf-8')).hexdigest()[:10]}.pdf"


def parse_chainage(text: str) -> tuple[Optional[float], Optional[float], str]:
    values: list[float] = []
    for token in CHAINAGE_RE.findall(clean(text)):
        num = re.search(r"\d{1,3}(?:\.\d{1,4})", token)
        if num:
            try:
                values.append(float(num.group(0)))
            except ValueError:
                pass
    # Avoid using speed values such as 40km/h as chainage if no decimals exist.
    values = [v for v in values if abs(v - int(v)) > 0.00001]
    if len(values) >= 2:
        return min(values[0], values[1]), max(values[0], values[1]), f"{values[0]:.3f}km to {values[1]:.3f}km"
    if len(values) == 1:
        return values[0], values[0], f"{values[0]:.3f}km"
    return None, None, ""


def split_location_distance(location_text: str) -> tuple[str, str, Optional[float], Optional[float]]:
    text = clean(location_text)
    from_km, to_km, distance = parse_chainage(text)
    if distance:
        first = re.search(r"\b\d{1,3}\.\d{1,4}\s*(?:km)?\b", text, flags=re.I)
        location = text[: first.start()].strip(" -") if first else text
        tail = text[first.start():].strip() if first else distance
        return location or text, tail, from_km, to_km
    return text, "", None, None


def infer_direction(*parts: object) -> str:
    text = " ".join(clean(p).lower() for p in parts if clean(p))
    if re.search(r"\b(up\s*&\s*down|up\s+and\s+down|bi|bidi|bi-directional|bidirectional|b-directional)\b", text):
        return "Bidirectional"
    if "yard" in text:
        return "Yard"
    if re.search(r"\bdown\b|\bdn\b", text):
        return "Down"
    if re.search(r"\bup\b", text):
        return "Up"
    return "Unknown"


def infer_line(*parts: object, direction: str = "Unknown") -> tuple[str, str]:
    text = " ".join(clean(p) for p in parts if clean(p))
    upper = text.upper()
    explicit = re.search(r"\b([A-Z]{2,6})(UP|DN|DM|BI|UM)?\b", upper)
    if explicit:
        prefix = explicit.group(1)
        suffix = explicit.group(2) or {"Up": "UP", "Down": "DN", "Bidirectional": "BI"}.get(direction, "")
        for known_prefix, line_name in LINE_PREFIXES.items():
            if prefix.startswith(known_prefix):
                return f"{known_prefix}{suffix}" if suffix else known_prefix, line_name
    lower = text.lower()
    for line_name, prefix, aliases in LINE_ALIASES:
        if any(alias in lower for alias in aliases) or line_name.lower() in lower:
            suffix = {"Up": "UP", "Down": "DN", "Bidirectional": "BI"}.get(direction, "")
            return f"{prefix}{suffix}" if suffix else prefix, line_name
    return "UNCLASSIFIED", "Unclassified"


def reason_group(reason: str) -> str:
    text = clean(reason).lower()
    if not text or BAD_REASON_RE.match(text):
        return "Invalid / shifted column"
    groups = [
        ("Signals / train control", ["signal", "signalling", "train control", "interlocking"]),
        ("Rail internal defect", ["ultrasonic", "internal"]),
        ("Rail surface defect", ["rail surface", "surface defect"]),
        ("Track geometry", ["track geometry", "geometry", "alignment"]),
        ("Turnout / points", ["turnout", "points", "switch", "wing rail"]),
        ("Structures / bridge", ["structural", "integrity", "bridge", "structure", "culvert"]),
        ("Level crossing / pedestrian crossing", ["pedestrian", "crossing"]),
        ("Noise mitigation", ["noise"]),
        ("Ballast / formation", ["ballast", "formation", "earthworks"]),
        ("New rail / weld", ["new rail", "weld", "re-rail", "rerail"]),
        ("Track condition", ["track", "rail", "sleeper", "curve wear", "rcf", "defect", "heat kick"]),
        ("Works / project", ["works", "project", "construction", "scope"]),
        ("Platform / station", ["platform", "station"]),
    ]
    for label, needles in groups:
        if any(n in text for n in needles):
            return label
    return "Other / review required"


def is_valid_reason_text(value: str) -> bool:
    text = clean(value)
    return bool(text) and not BAD_REASON_RE.match(text)


def split_after_speed(after_speed: str, notice_year: Optional[int]) -> tuple[str, str, str]:
    text = clean(after_speed)
    if not text:
        return "", "", ""
    date_imposed = ""
    date_match = DATE_TOKEN_RE.search(text)
    if date_match:
        candidate = date_match.group(0)
        if parse_date(candidate, notice_year):
            date_imposed = candidate
            text = clean(text[: date_match.start()] + " " + text[date_match.end():])
    cancel = ""
    token_matches = list(CANCEL_TOKEN_RE.finditer(text))
    date_matches = list(DATE_TOKEN_RE.finditer(text))
    candidates = []
    if token_matches:
        candidates.append(token_matches[-1])
    if date_matches:
        candidates.append(date_matches[-1])
    if candidates:
        last = max(candidates, key=lambda m: m.start())
        # Treat last token/date as cancellation only if it is near the end.
        if last.end() >= len(text) - 4:
            cancel = last.group(0)
            text = clean(text[: last.start()] + " " + text[last.end():])
    reason = clean(text).strip(" -|;")
    return date_imposed, reason, cancel


def repair_cells_to_fields(cells: list[str], notice_date: str) -> dict[str, str]:
    cells = [clean(c) for c in cells]
    year = int(notice_date[:4]) if notice_date else None
    speed, speed_idx = extract_speed_from_cells(cells)
    if not speed:
        return {}
    if len(cells) >= 6 and speed_idx is not None:
        location = cells[0]
        stn_no = cells[1] if len(cells) > 1 else ""
        date_imposed = cells[3] if len(cells) > 3 else ""
        reason = cells[4] if len(cells) > 4 else ""
        cancel = cells[5] if len(cells) > 5 else ""
        if speed_idx != 2:
            left = " ".join(cells[:speed_idx])
            right = " ".join(cells[speed_idx + 1:])
            s_date, s_reason, s_cancel = split_after_speed(right, year)
            location = left or cells[0]
            stn_no = STN_RE.search(left).group(0) if STN_RE.search(left) else ""
            date_imposed = s_date or date_imposed
            reason = s_reason or reason
            cancel = s_cancel or cancel
        return {
            "location": location,
            "stn_no": stn_no,
            "max_speed": speed,
            "date_imposed": date_imposed,
            "reason": reason,
            "date_cancelled": cancel,
        }
    joined = " | ".join(cells)
    return repair_text_line_to_fields(joined, notice_date)


def repair_text_line_to_fields(line: str, notice_date: str) -> dict[str, str]:
    text = clean(line).strip("|")
    year = int(notice_date[:4]) if notice_date else None
    match = SPEED_RE.search(text)
    if not match:
        return {}
    speed = normalise_speed(match.group(0))
    before = clean(text[: match.start()].replace("|", " "))
    after = clean(text[match.end():].replace("|", " "))
    stn_match = STN_RE.search(before)
    stn_no = stn_match.group(0) if stn_match else ""
    location = clean(STN_RE.sub("", before)) if stn_match else before
    date_imposed, reason, cancel = split_after_speed(after, year)
    return {
        "location": location,
        "stn_no": stn_no,
        "max_speed": speed,
        "date_imposed": date_imposed,
        "reason": reason,
        "date_cancelled": cancel,
    }


def fields_to_row(notice_date: str, fields: dict[str, str], candidate: CandidateRow) -> tuple[Optional[ExtractedRow], Optional[RejectedRow], bool]:
    cells = [clean(c) for c in candidate.cells]
    if is_blank_cells(cells):
        return None, None, True
    if not fields:
        category = "parser_artifact" if is_low_content_fragment(cells) else "manual_review_required"
        reason = "fragment_row_artifact" if category == "parser_artifact" else "missing_or_invalid_speed"
        return None, RejectedRow(notice_date, candidate.source_page, candidate.source_row_number, reason, category, json.dumps(cells, ensure_ascii=False), "ignore_or_stitch_fragment"), category == "parser_artifact"

    loc_text = clean(fields.get("location"))
    speed = normalise_speed(fields.get("max_speed", ""))
    reason = clean(fields.get("reason"))
    if not loc_text or not speed:
        category = "parser_artifact" if is_low_content_fragment(cells) else "manual_review_required"
        return None, RejectedRow(notice_date, candidate.source_page, candidate.source_row_number, "missing_location_or_speed", category, json.dumps(cells, ensure_ascii=False), "manual_review"), category == "parser_artifact"
    if not is_valid_reason_text(reason):
        return None, RejectedRow(notice_date, candidate.source_page, candidate.source_row_number, "invalid_reason_or_shifted_columns", "manual_review_required", json.dumps(cells, ensure_ascii=False), "manual_review_probable_shifted_columns"), False

    location, distance, from_km, to_km = split_location_distance(loc_text)
    direction = infer_direction(loc_text, distance)
    line_key, line_name = infer_line(loc_text, distance, direction=direction)
    affected_area = clean(f"{location} {distance}") or loc_text
    notice_year = int(notice_date[:4]) if notice_date else None
    imposed_raw = clean(fields.get("date_imposed"))
    cancel_raw = clean(fields.get("date_cancelled"))
    imposed_norm = parse_date(imposed_raw, notice_year)
    cancel_norm = parse_date(cancel_raw, notice_year)
    notes: list[str] = [f"strategy={candidate.strategy}"]
    if not distance:
        notes.append("no_numeric_chainage_supplied")
    if line_key == "UNCLASSIFIED":
        notes.append("line_not_inferred")
    if direction == "Unknown":
        notes.append("direction_not_inferred")
    if imposed_raw and not imposed_norm:
        notes.append("date_imposed_not_normalised")
    if normalise_speed(fields.get("max_speed", "")) != clean(fields.get("max_speed", "")):
        notes.append("speed_normalised")
    group = reason_group(reason)
    quality = "repaired" if any(n in ";".join(notes) for n in ["normalised", "strategy=text", "no_numeric"]) else "valid"
    return ExtractedRow(
        notice_date=notice_date,
        location=location,
        line_section=location,
        distance_km=distance,
        stn_no=clean(fields.get("stn_no")),
        max_speed=speed,
        date_imposed_raw=imposed_raw,
        date_imposed_normalised=imposed_norm,
        reason_raw=reason,
        reason_group=group,
        date_cancelled_raw=cancel_raw,
        date_cancelled_normalised=cancel_norm,
        line_key=line_key,
        line_name=line_name,
        location_direction=direction,
        affected_area=affected_area,
        location_from_km=from_km,
        location_to_km=to_km,
        source_page=candidate.source_page,
        source_row_number=candidate.source_row_number,
        extraction_strategy=candidate.strategy,
        row_quality=quality,
        normalisation_notes="; ".join(notes),
        raw_row_json=json.dumps(cells, ensure_ascii=False),
    ), None, False


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def ensure_column(conn: sqlite3.Connection, table: str, col: str, spec: str) -> None:
    if col not in table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {spec}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS source_pdf (
        source_pdf_id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL UNIQUE,
        filename TEXT NOT NULL,
        local_path TEXT,
        url_hash TEXT NOT NULL,
        file_sha256 TEXT,
        notice_date TEXT,
        discovered_at TEXT NOT NULL,
        downloaded_at TEXT,
        processed_at TEXT,
        status TEXT NOT NULL DEFAULT 'discovered',
        last_error TEXT,
        page_count INTEGER,
        tsr_table_count INTEGER DEFAULT 0,
        tsr_row_count INTEGER DEFAULT 0,
        rejected_row_count INTEGER DEFAULT 0,
        artifact_row_count INTEGER DEFAULT 0,
        extraction_strategy TEXT
    );
    CREATE TABLE IF NOT EXISTS tsr_master (
        tsr_master_id INTEGER PRIMARY KEY AUTOINCREMENT,
        master_fingerprint TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tsr_occurrence (
        tsr_record_id INTEGER PRIMARY KEY AUTOINCREMENT,
        tsr_master_id INTEGER NOT NULL,
        source_pdf_id INTEGER NOT NULL,
        notice_date TEXT NOT NULL,
        location TEXT,
        line_section TEXT,
        distance_km TEXT,
        stn_no TEXT,
        max_speed TEXT,
        date_imposed TEXT,
        date_imposed_normalised TEXT,
        reason TEXT,
        reason_group TEXT,
        date_cancelled TEXT,
        date_cancelled_normalised TEXT,
        line_key TEXT,
        line_name TEXT,
        location_direction TEXT,
        affected_area TEXT,
        location_from_km REAL,
        location_to_km REAL,
        source_page INTEGER,
        source_row_number INTEGER,
        row_fingerprint TEXT NOT NULL,
        raw_row_json TEXT,
        row_quality TEXT,
        normalisation_notes TEXT,
        extraction_strategy TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (tsr_master_id) REFERENCES tsr_master(tsr_master_id),
        FOREIGN KEY (source_pdf_id) REFERENCES source_pdf(source_pdf_id),
        UNIQUE (source_pdf_id, row_fingerprint)
    );
    CREATE TABLE IF NOT EXISTS tsr_rejected_row (
        rejected_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_pdf_id INTEGER NOT NULL,
        notice_date TEXT,
        source_page INTEGER,
        source_row_number INTEGER,
        reject_reason TEXT,
        rejection_category TEXT,
        suggested_action TEXT,
        raw_row_json TEXT,
        created_at TEXT NOT NULL,
        review_status TEXT DEFAULT 'unreviewed',
        FOREIGN KEY (source_pdf_id) REFERENCES source_pdf(source_pdf_id)
    );
    CREATE TABLE IF NOT EXISTS tsr_artifact_row (
        artifact_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_pdf_id INTEGER NOT NULL,
        notice_date TEXT,
        source_page INTEGER,
        source_row_number INTEGER,
        artifact_reason TEXT,
        raw_row_json TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (source_pdf_id) REFERENCES source_pdf(source_pdf_id)
    );
    CREATE INDEX IF NOT EXISTS idx_occ_notice ON tsr_occurrence(notice_date);
    CREATE INDEX IF NOT EXISTS idx_occ_master ON tsr_occurrence(tsr_master_id);
    CREATE INDEX IF NOT EXISTS idx_source_status ON source_pdf(status);
    """)
    for col, spec in {
        "artifact_row_count": "INTEGER DEFAULT 0", "extraction_strategy": "TEXT",
    }.items():
        ensure_column(conn, "source_pdf", col, spec)
    for col, spec in {
        "date_imposed_normalised": "TEXT", "reason_group": "TEXT", "date_cancelled_normalised": "TEXT",
        "line_key": "TEXT", "line_name": "TEXT", "location_direction": "TEXT", "affected_area": "TEXT",
        "location_from_km": "REAL", "location_to_km": "REAL", "row_quality": "TEXT", "normalisation_notes": "TEXT",
        "extraction_strategy": "TEXT",
    }.items():
        ensure_column(conn, "tsr_occurrence", col, spec)
    for col, spec in {"rejection_category": "TEXT", "suggested_action": "TEXT", "review_status": "TEXT DEFAULT 'unreviewed'"}.items():
        ensure_column(conn, "tsr_rejected_row", col, spec)
    conn.commit()


def extract_dnn_page_config(html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, pat in {"module_id": r'moduleId:\s*"(\d+)"', "root_folder_id": r'rootFolderId:\s*"(\d+)"'}.items():
        m = re.search(pat, html, re.I)
        if m:
            out[key] = m.group(1)
    m = re.search(r'name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)["\']', html, re.I)
    if m:
        out["request_verification_token"] = m.group(1)
    return out


def create_dnn_session(seed_url: str, module_id: str, tab_id: str):
    session = requests.Session()
    session.headers.update({
        "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": seed_url,
        "moduleid": str(module_id),
        "tabid": str(tab_id),
    })
    response = session.get(seed_url, timeout=45)
    response.raise_for_status()
    cfg = extract_dnn_page_config(response.text)
    session.headers.update({"moduleid": cfg.get("module_id", module_id), "tabid": cfg.get("tab_id", tab_id)})
    if cfg.get("request_verification_token"):
        session.headers.update({"requestverificationtoken": cfg["request_verification_token"]})
    return session


def get_dnn_folder_content(session: Any, folder_id: int) -> dict[str, object]:
    response = session.get(DNN_CONTENT_ENDPOINT, params={"startIndex": 0, "numItems": 2147483647, "sort": "Name asc", "folderId": folder_id}, timeout=60)
    response.raise_for_status()
    data = response.json()
    (DIAGNOSTICS_DIR / f"dnn_folder_{folder_id}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data if isinstance(data, dict) else {}


def dnn_item_to_pdf(item: dict[str, object]) -> Optional[DiscoveredPdf]:
    if bool(item.get("IsFolder")):
        return None
    name = clean(item.get("Name"))
    url = clean(item.get("Url"))
    ext = clean(item.get("Extension")).lower().lstrip(".")
    if ext != "pdf" or not url or not re.search(r"weekly\s*notice", name, re.I):
        return None
    full = urljoin(PTA_BASE_URL, url)
    return DiscoveredPdf(full, safe_filename(full), notice_date_from_url(full))


def discover_dnn_pdfs(seed_url: str, folder_ids: list[int], module_id: str, tab_id: str) -> list[DiscoveredPdf]:
    session = create_dnn_session(seed_url, module_id, tab_id)
    queue = list(dict.fromkeys(folder_ids or DEFAULT_DNN_FOLDER_IDS))
    seen: set[int] = set()
    found: dict[str, DiscoveredPdf] = {}
    while queue:
        folder_id = queue.pop(0)
        if folder_id in seen:
            continue
        seen.add(folder_id)
        data = get_dnn_folder_content(session, folder_id)
        for item in data.get("Items", []):
            if not isinstance(item, dict):
                continue
            if item.get("IsFolder"):
                try:
                    queue.append(int(item.get("ItemId")))
                except Exception:
                    pass
            else:
                pdf = dnn_item_to_pdf(item)
                if pdf:
                    found[pdf.url] = pdf
        logging.info("DNN folder %s total weekly_notice_pdfs=%s", folder_id, len(found))
        time.sleep(0.15)
    return sorted(found.values(), key=lambda p: (p.notice_date or "9999-99-99", p.url))


def register_pdfs(conn: sqlite3.Connection, pdfs: Iterable[DiscoveredPdf]) -> int:
    n = 0
    for pdf in pdfs:
        conn.execute(
            """INSERT INTO source_pdf (url, filename, url_hash, notice_date, discovered_at, status)
               VALUES (?, ?, ?, ?, ?, 'discovered')
               ON CONFLICT(url) DO UPDATE SET filename=excluded.filename, notice_date=COALESCE(source_pdf.notice_date, excluded.notice_date)""",
            (pdf.url, pdf.filename, hashlib.sha256(pdf.url.encode()).hexdigest(), pdf.notice_date, now_iso()),
        )
        n += 1
    conn.commit()
    return n


def download_candidates(url: str) -> list[str]:
    split = urlsplit(url)
    path = unquote(split.path)
    variants = [path]
    for old, new in {"Febuary": "February", "Week Ending- ": "Week Ending - ", "Week Ending-": "Week Ending -"}.items():
        if old in path:
            variants.append(path.replace(old, new))
    out: list[str] = []
    for variant in variants:
        encoded = quote(variant, safe="/%")
        out.append(urlunsplit((split.scheme, split.netloc, encoded, split.query, split.fragment)))
        out.append(urlunsplit((split.scheme, split.netloc, encoded, "", split.fragment)))
    return list(dict.fromkeys(out))


def download_source_pdf(conn: sqlite3.Connection, source: sqlite3.Row, force: bool = False) -> Path:
    local = PDF_DIR / source["filename"]
    if local.exists() and not force:
        return local
    last_error = ""
    for candidate in download_candidates(source["url"]):
        try:
            response = requests.get(candidate, timeout=120, allow_redirects=True)
        except Exception as exc:
            last_error = str(exc)
            continue
        if response.status_code == 404:
            last_error = f"404 Not Found: {candidate}"
            continue
        try:
            response.raise_for_status()
        except Exception as exc:
            last_error = str(exc)
            continue
        if not response.content.startswith(b"%PDF"):
            last_error = f"Downloaded content is not a PDF from {candidate}"
            continue
        local.write_bytes(response.content)
        conn.execute("UPDATE source_pdf SET local_path=?, file_sha256=?, downloaded_at=?, status='downloaded', last_error=NULL WHERE source_pdf_id=?", (str(local), hashlib.sha256(response.content).hexdigest(), now_iso(), source["source_pdf_id"]))
        conn.commit()
        return local
    raise MissingPdfError(f"Unable to download PDF. Last error: {last_error}")


def page_has_tsr_section(page: Any) -> bool:
    text = page.extract_text() or ""
    return bool(re.search(r"Current\s+(?:Temporary\s+)?Speed\s+Restrictions|Temporary\s+Speed\s+Restrictions|Date\s+to\s+be\s+Cancelled|Maximum\s+Speed", text, re.I))


def table_candidates(page: Any, strategy_name: str, settings: dict[str, Any]) -> list[CandidateRow]:
    out: list[CandidateRow] = []
    try:
        tables = page.extract_tables(table_settings=settings) or []
    except Exception:
        return out
    row_no = 0
    for table in tables:
        for raw in table:
            cells = [clean(c) for c in (raw or [])]
            if not cells:
                continue
            text = " ".join(cells).lower()
            if re.search(r"location\s+to\s+and\s+from|maximum\s+speed|date\s+imposed|date\s+to\s+be\s+cancelled", text):
                continue
            # Collapse/extend to 6 cells for downstream compatibility.
            if len(cells) < 6:
                cells = cells + [""] * (6 - len(cells))
            elif len(cells) > 6:
                cells = cells[:5] + [" ".join(cells[5:])]
            row_no += 1
            out.append(CandidateRow(cells, page.page_number, row_no, strategy_name, " | ".join(cells)))
    return out


def text_line_candidates(page: Any) -> list[CandidateRow]:
    text = page.extract_text(x_tolerance=2, y_tolerance=3) or ""
    lines = [clean(line) for line in text.splitlines()]
    out: list[CandidateRow] = []
    row_no = 0
    for i, line in enumerate(lines):
        if not line or re.search(r"Weekly Notice|Page \d+|Temporary Speed Restrictions|Maximum Speed|Date to be Cancelled", line, re.I):
            continue
        # Include stitched previous/next lines around speed-bearing rows to rescue wrapped table rows.
        stitched = line
        if SPEED_RE.search(line):
            if i > 0 and not SPEED_RE.search(lines[i - 1]) and not re.search(r"Maximum Speed|Date", lines[i - 1], re.I):
                stitched = f"{lines[i-1]} {stitched}"
            if i + 1 < len(lines) and not SPEED_RE.search(lines[i + 1]) and not re.search(r"Maximum Speed|Date", lines[i + 1], re.I):
                # Only append next line when current line appears incomplete.
                if len(DATE_TOKEN_RE.findall(stitched)) == 0 or not re.search(r"Rail|Track|Signal|Point|Crossing|Defect|Sleeper|Geometry|Noise|Works|Turnout", stitched, re.I):
                    stitched = f"{stitched} {lines[i+1]}"
            row_no += 1
            out.append(CandidateRow([stitched, "", "", "", "", ""], page.page_number, row_no, "text_line", stitched))
    return out


def evaluate_candidates(candidates: list[CandidateRow], notice_date: str, strategy: str) -> StrategyResult:
    rows: list[ExtractedRow] = []
    rejects: list[RejectedRow] = []
    artifacts = 0
    seen: set[str] = set()
    for cand in candidates:
        cells = [clean(c) for c in cand.cells]
        key = re.sub(r"\s+", " ", "|".join(cells)).lower()
        if key in seen:
            continue
        seen.add(key)
        if is_blank_cells(cells):
            artifacts += 1
            continue
        fields = repair_cells_to_fields(cells, notice_date) if cand.strategy != "text_line" else repair_text_line_to_fields(cand.raw_text, notice_date)
        row, reject, artifact = fields_to_row(notice_date, fields, cand)
        if artifact:
            artifacts += 1
        elif row:
            rows.append(row)
        elif reject:
            rejects.append(reject)
    return StrategyResult(strategy, rows, rejects, artifacts, len(candidates))


def extract_rows_from_pdf(pdf_path: Path, notice_date: str) -> tuple[list[ExtractedRow], list[RejectedRow], int, int, int, str]:
    all_rows: list[ExtractedRow] = []
    all_rejects: list[RejectedRow] = []
    artifact_count = 0
    table_count = 0
    strategy_counts: dict[str, int] = {}
    with pdfplumber.open(str(pdf_path)) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            if not page_has_tsr_section(page):
                continue
            table_count += 1
            strategies: list[StrategyResult] = []
            table_settings = [
                ("table_lines", {"vertical_strategy": "lines", "horizontal_strategy": "lines", "snap_tolerance": 3, "join_tolerance": 3, "intersection_tolerance": 5}),
                ("table_text", {"vertical_strategy": "text", "horizontal_strategy": "text", "snap_tolerance": 3, "join_tolerance": 3, "intersection_tolerance": 5}),
            ]
            for name, settings in table_settings:
                strategies.append(evaluate_candidates(table_candidates(page, name, settings), notice_date, name))
            strategies.append(evaluate_candidates(text_line_candidates(page), notice_date, "text_line"))
            best = max(strategies, key=lambda s: s.score)
            strategy_counts[best.strategy] = strategy_counts.get(best.strategy, 0) + 1
            all_rows.extend(best.rows)
            all_rejects.extend(best.rejects)
            artifact_count += best.artifact_count
            logging.debug("%s page %s best_strategy=%s accepted=%s rejected=%s artifacts=%s candidates=%s", pdf_path.name, page.page_number, best.strategy, len(best.rows), len(best.rejects), best.artifact_count, best.candidate_count)
    used_strategy = ";".join(f"{k}:{v}" for k, v in sorted(strategy_counts.items()))
    return all_rows, all_rejects, artifact_count, page_count, table_count, used_strategy


def master_fingerprint(row: ExtractedRow) -> str:
    material = "|".join([
        row.line_key,
        f"{row.location_from_km or ''}",
        f"{row.location_to_km or ''}",
        norm_key(row.stn_no),
        row.date_imposed_normalised or norm_key(row.date_imposed_raw),
        norm_key(row.reason_raw),
    ])
    return hashlib.sha256(material.encode()).hexdigest()


def row_fingerprint(row: ExtractedRow) -> str:
    material = "|".join([
        row.line_key, row.affected_area, norm_key(row.stn_no), norm_key(row.max_speed), row.date_imposed_normalised,
        norm_key(row.reason_raw), row.date_cancelled_normalised, str(row.source_page), str(row.source_row_number), row.extraction_strategy,
    ])
    return hashlib.sha256(material.encode()).hexdigest()


def upsert_master(conn: sqlite3.Connection, row: ExtractedRow) -> int:
    fingerprint = master_fingerprint(row)
    stamp = now_iso()
    conn.execute(
        """INSERT INTO tsr_master (master_fingerprint, created_at, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(master_fingerprint) DO UPDATE SET updated_at=excluded.updated_at""",
        (fingerprint, stamp, stamp),
    )
    return int(conn.execute("SELECT tsr_master_id FROM tsr_master WHERE master_fingerprint=?", (fingerprint,)).fetchone()[0])


def insert_occurrence(conn: sqlite3.Connection, source_pdf_id: int, row: ExtractedRow) -> None:
    mid = upsert_master(conn, row)
    conn.execute(
        """INSERT OR IGNORE INTO tsr_occurrence (
            tsr_master_id, source_pdf_id, notice_date, location, line_section, distance_km, stn_no, max_speed,
            date_imposed, date_imposed_normalised, reason, reason_group, date_cancelled, date_cancelled_normalised,
            line_key, line_name, location_direction, affected_area, location_from_km, location_to_km,
            source_page, source_row_number, row_fingerprint, raw_row_json, row_quality, normalisation_notes, extraction_strategy, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            mid, source_pdf_id, row.notice_date, row.location, row.line_section, row.distance_km, row.stn_no, row.max_speed,
            row.date_imposed_raw, row.date_imposed_normalised, row.reason_raw, row.reason_group, row.date_cancelled_raw,
            row.date_cancelled_normalised, row.line_key, row.line_name, row.location_direction, row.affected_area,
            row.location_from_km, row.location_to_km, row.source_page, row.source_row_number, row_fingerprint(row),
            row.raw_row_json, row.row_quality, row.normalisation_notes, row.extraction_strategy, now_iso(),
        ),
    )


def process_source_pdf(conn: sqlite3.Connection, source: sqlite3.Row, force: bool = False) -> None:
    if source["status"] == "processed" and not force:
        logging.info("Skipping previously processed PDF: %s", source["filename"])
        return
    sid = int(source["source_pdf_id"])
    try:
        path = download_source_pdf(conn, source)
        notice_date = source["notice_date"] or notice_date_from_url(source["url"])
        if not notice_date:
            raise PipelineError(f"Unable to determine notice date for {source['filename']}")
        if force:
            conn.execute("DELETE FROM tsr_occurrence WHERE source_pdf_id=?", (sid,))
            conn.execute("DELETE FROM tsr_rejected_row WHERE source_pdf_id=?", (sid,))
            conn.execute("DELETE FROM tsr_artifact_row WHERE source_pdf_id=?", (sid,))
        rows, rejects, artifacts, page_count, table_count, strategy = extract_rows_from_pdf(path, notice_date)
        for reject in rejects:
            conn.execute(
                """INSERT INTO tsr_rejected_row (source_pdf_id, notice_date, source_page, source_row_number, reject_reason, rejection_category, suggested_action, raw_row_json, created_at, review_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unreviewed')""",
                (sid, reject.notice_date, reject.source_page, reject.source_row_number, reject.reject_reason, reject.rejection_category, reject.suggested_action, reject.raw_row_json, now_iso()),
            )
        for row in rows:
            insert_occurrence(conn, sid, row)
        if artifacts:
            conn.execute(
                """INSERT INTO tsr_artifact_row (source_pdf_id, notice_date, source_page, source_row_number, artifact_reason, raw_row_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (sid, notice_date, 0, 0, f"{artifacts} parser artifact rows skipped", "", now_iso()),
            )
        conn.execute(
            """UPDATE source_pdf
               SET notice_date=COALESCE(?, notice_date), processed_at=?, status='processed', last_error=NULL,
                   page_count=?, tsr_table_count=?, tsr_row_count=?, rejected_row_count=?, artifact_row_count=?, extraction_strategy=?
               WHERE source_pdf_id=?""",
            (notice_date, now_iso(), page_count, table_count, len(rows), len(rejects), artifacts, strategy, sid),
        )
        conn.commit()
        logging.info("Processed %s: accepted=%s rejected=%s artifacts=%s strategy=%s", source["filename"], len(rows), len(rejects), artifacts, strategy)
    except MissingPdfError as exc:
        conn.rollback()
        conn.execute("UPDATE source_pdf SET status='missing', last_error=? WHERE source_pdf_id=?", (str(exc), sid))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logging.exception("Failed processing %s", source["filename"])
        conn.execute("UPDATE source_pdf SET status='failed', last_error=? WHERE source_pdf_id=?", (str(exc), sid))
        conn.commit()


def latest_notice_date(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(notice_date) AS d FROM source_pdf WHERE status='processed'").fetchone()
    return row["d"] or ""


def query_rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def write_csv(path: Path, rows: list[sqlite3.Row] | list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        if not rows:
            handle.write("")
            return
        first = rows[0]
        fieldnames = list(first.keys()) if hasattr(first, "keys") else list(first)
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows([dict(row) for row in rows])


def validate_before_export(conn: sqlite3.Connection) -> None:
    total = conn.execute("SELECT COUNT(*) AS c FROM tsr_occurrence").fetchone()["c"]
    if total == 0:
        raise PipelineError("Refusing to export analytics: no accepted TSR rows exist.")
    latest = latest_notice_date(conn)
    if latest:
        latest_count = conn.execute("SELECT COUNT(*) AS c FROM tsr_occurrence WHERE notice_date=?", (latest,)).fetchone()["c"]
        latest_source = conn.execute("SELECT COALESCE(rejected_row_count,0) AS r, COALESCE(artifact_row_count,0) AS a FROM source_pdf WHERE status='processed' AND notice_date=? ORDER BY source_pdf_id DESC LIMIT 1", (latest,)).fetchone()
        if latest_count == 0:
            raise PipelineError(f"Refusing to export analytics: latest processed notice {latest} has zero accepted TSR rows.")
        if latest_source and latest_source["r"] > 0 and latest_count == 0:
            raise PipelineError(f"Refusing to export analytics: latest processed notice {latest} generated only rejected/artifact rows.")
    missing = conn.execute(
        """SELECT COUNT(*) AS c FROM tsr_occurrence
           WHERE COALESCE(NULLIF(TRIM(line_key), ''), '') = ''
              OR COALESCE(NULLIF(TRIM(line_name), ''), '') = ''
              OR COALESCE(NULLIF(TRIM(location_direction), ''), '') = ''
              OR COALESCE(NULLIF(TRIM(affected_area), ''), '') = ''
              OR COALESCE(NULLIF(TRIM(reason_group), ''), '') = ''"""
    ).fetchone()["c"]
    if missing:
        raise PipelineError(f"Refusing to export analytics: {missing} accepted rows are missing required normalised fields.")


def export_csvs(conn: sqlite3.Connection) -> None:
    validate_before_export(conn)
    latest = latest_notice_date(conn)
    write_csv(EXPORT_DIR / "pta_tsr_occurrences.csv", query_rows(conn, """
        SELECT o.tsr_record_id, o.tsr_master_id, o.notice_date,
               o.line_key, o.line_name, o.location_direction, o.affected_area,
               o.location, o.line_section, o.distance_km, o.location_from_km, o.location_to_km,
               o.stn_no, o.max_speed,
               o.date_imposed AS date_imposed_raw, o.date_imposed_normalised,
               o.reason AS reason_raw, o.reason_group,
               o.date_cancelled AS date_cancelled_raw, o.date_cancelled_normalised,
               s.filename AS source_pdf, s.url AS source_url,
               o.source_page, o.source_row_number, o.extraction_strategy, o.row_quality, o.normalisation_notes
        FROM tsr_occurrence o JOIN source_pdf s ON s.source_pdf_id=o.source_pdf_id
        ORDER BY o.notice_date, o.tsr_record_id
    """))
    write_csv(EXPORT_DIR / "pta_tsr_masters.csv", query_rows(conn, f"""
        WITH summary AS (
            SELECT tsr_master_id, MIN(notice_date) AS first_seen_notice_date, MAX(notice_date) AS last_seen_notice_date, COUNT(*) AS weeks_seen
            FROM tsr_occurrence GROUP BY tsr_master_id
        ), latest_occ AS (
            SELECT o.*, ROW_NUMBER() OVER (PARTITION BY tsr_master_id ORDER BY notice_date DESC, tsr_record_id DESC) AS rn
            FROM tsr_occurrence o
        )
        SELECT s.tsr_master_id, s.first_seen_notice_date, s.last_seen_notice_date, s.weeks_seen,
               CASE WHEN s.last_seen_notice_date='{latest}' THEN 1 ELSE 0 END AS is_active,
               CASE WHEN s.last_seen_notice_date='{latest}' THEN '' ELSE s.last_seen_notice_date END AS resolved_date,
               l.line_key, l.line_name, l.location_direction, l.affected_area,
               l.location AS latest_location, l.line_section AS latest_line_section, l.distance_km AS latest_distance_km,
               l.location_from_km, l.location_to_km, l.stn_no AS latest_stn_no, l.max_speed AS latest_max_speed,
               l.date_imposed AS date_imposed_raw, l.date_imposed_normalised,
               l.reason AS latest_reason_raw, l.reason_group AS latest_reason_group,
               l.date_cancelled AS latest_date_cancelled_raw,
               CASE WHEN s.last_seen_notice_date='{latest}' THEN '' ELSE COALESCE(NULLIF(l.date_cancelled_normalised,''), s.last_seen_notice_date) END AS latest_date_cancelled,
               m.master_fingerprint, l.row_quality, l.normalisation_notes
        FROM summary s
        JOIN latest_occ l ON l.tsr_master_id=s.tsr_master_id AND l.rn=1
        JOIN tsr_master m ON m.tsr_master_id=s.tsr_master_id
        ORDER BY s.first_seen_notice_date, s.tsr_master_id
    """))
    write_csv(EXPORT_DIR / "pta_tsr_source_pdfs.csv", query_rows(conn, """
        SELECT source_pdf_id, notice_date, filename, url, status, tsr_table_count, tsr_row_count, rejected_row_count, artifact_row_count, extraction_strategy, last_error
        FROM source_pdf ORDER BY notice_date, filename
    """))
    write_csv(EXPORT_DIR / "pta_tsr_rejected_rows.csv", query_rows(conn, """
        SELECT r.rejected_row_id, r.notice_date, s.filename AS source_pdf, r.source_page, r.source_row_number,
               r.reject_reason, r.rejection_category, r.suggested_action, r.review_status, r.raw_row_json
        FROM tsr_rejected_row r JOIN source_pdf s ON s.source_pdf_id=r.source_pdf_id
        ORDER BY r.notice_date, s.filename, r.source_page, r.source_row_number
    """))
    export_analytics(conn, latest)


def export_analytics(conn: sqlite3.Connection, latest: str) -> None:
    write_csv(ANALYTICS_DIR / "pta_tsr_active_current.csv", query_rows(conn, """
        SELECT notice_date, line_key, line_name, location_direction, affected_area, location, distance_km, location_from_km, location_to_km,
               stn_no, max_speed, date_imposed_normalised, reason AS reason_raw, reason_group, date_cancelled AS date_cancelled_raw,
               date_cancelled_normalised, source_page, source_row_number, extraction_strategy, row_quality, normalisation_notes
        FROM tsr_occurrence WHERE notice_date=? ORDER BY source_page, source_row_number
    """, (latest,)))
    write_csv(ANALYTICS_DIR / "pta_tsr_active_by_line.csv", query_rows(conn, """
        SELECT line_key, line_name, location_direction, COUNT(*) AS active_tsr_count
        FROM tsr_occurrence WHERE notice_date=? GROUP BY line_key, line_name, location_direction ORDER BY active_tsr_count DESC, line_key
    """, (latest,)))
    write_csv(ANALYTICS_DIR / "pta_tsr_active_by_cause.csv", query_rows(conn, """
        SELECT reason_group, COUNT(*) AS active_tsr_count
        FROM tsr_occurrence WHERE notice_date=? GROUP BY reason_group ORDER BY active_tsr_count DESC, reason_group
    """, (latest,)))
    write_csv(ANALYTICS_DIR / "pta_tsr_notice_snapshot_summary.csv", query_rows(conn, """
        SELECT notice_date, substr(notice_date,1,4) AS year, substr(notice_date,6,2) AS month, substr(notice_date,1,7) AS year_month,
               line_key, line_name, location_direction, COUNT(*) AS notice_tsr_count, COUNT(DISTINCT tsr_master_id) AS distinct_tsr_count
        FROM tsr_occurrence GROUP BY notice_date, line_key, line_name, location_direction ORDER BY notice_date, line_key
    """))
    write_csv(ANALYTICS_DIR / "pta_tsr_active_by_notice.csv", query_rows(conn, """
        SELECT notice_date, substr(notice_date,1,4) AS year, substr(notice_date,6,2) AS month, substr(notice_date,1,7) AS year_month,
               line_key, line_name, location_direction, COUNT(*) AS notice_tsr_count, COUNT(DISTINCT tsr_master_id) AS distinct_tsr_count
        FROM tsr_occurrence GROUP BY notice_date, line_key, line_name, location_direction ORDER BY notice_date, line_key
    """))
    write_csv(ANALYTICS_DIR / "pta_tsr_line_month_summary.csv", query_rows(conn, """
        SELECT substr(notice_date,1,4) AS year, substr(notice_date,6,2) AS month, substr(notice_date,1,7) AS year_month,
               line_key, line_name, location_direction, COUNT(*) AS restriction_occurrences, COUNT(DISTINCT tsr_master_id) AS distinct_tsr_count
        FROM tsr_occurrence GROUP BY year, month, year_month, line_key, line_name, location_direction ORDER BY year_month, line_key
    """))
    write_csv(ANALYTICS_DIR / "pta_tsr_reason_summary.csv", query_rows(conn, """
        SELECT reason_group, reason AS reason_raw, COUNT(*) AS restriction_occurrences, COUNT(DISTINCT tsr_master_id) AS distinct_tsr_count
        FROM tsr_occurrence GROUP BY reason_group, reason ORDER BY restriction_occurrences DESC, reason_group
    """))
    write_csv(ANALYTICS_DIR / "pta_tsr_segment_summary.csv", query_rows(conn, """
        SELECT line_key, line_name, location_direction, affected_area, location_from_km, location_to_km,
               COUNT(*) AS restriction_occurrences, COUNT(DISTINCT tsr_master_id) AS distinct_tsr_count,
               MIN(notice_date) AS first_seen_notice_date, MAX(notice_date) AS last_seen_notice_date
        FROM tsr_occurrence
        GROUP BY line_key, line_name, location_direction, affected_area, location_from_km, location_to_km
        ORDER BY restriction_occurrences DESC, line_key, affected_area
    """))
    write_csv(ANALYTICS_DIR / "pta_tsr_duration_buckets.csv", query_rows(conn, f"""
        WITH summary AS (
            SELECT tsr_master_id, MIN(notice_date) AS first_seen_notice_date, MAX(notice_date) AS last_seen_notice_date, COUNT(*) AS weeks_seen
            FROM tsr_occurrence GROUP BY tsr_master_id
        ), latest_occ AS (
            SELECT o.*, ROW_NUMBER() OVER (PARTITION BY tsr_master_id ORDER BY notice_date DESC, tsr_record_id DESC) AS rn
            FROM tsr_occurrence o
        )
        SELECT s.tsr_master_id, l.line_key, l.line_name, l.location_direction, l.affected_area,
               l.reason AS latest_reason_raw, l.reason_group,
               s.first_seen_notice_date, s.last_seen_notice_date, s.weeks_seen,
               CASE WHEN s.last_seen_notice_date='{latest}' THEN 1 ELSE 0 END AS is_active,
               CASE WHEN s.last_seen_notice_date='{latest}' THEN '' ELSE s.last_seen_notice_date END AS resolved_date,
               CASE WHEN s.weeks_seen<=1 THEN '1 week'
                    WHEN s.weeks_seen<=4 THEN '2-4 weeks'
                    WHEN s.weeks_seen<=13 THEN '1-3 months'
                    WHEN s.weeks_seen<=26 THEN '3-6 months'
                    WHEN s.weeks_seen<=52 THEN '6-12 months'
                    WHEN s.weeks_seen<=104 THEN '1-2 years'
                    WHEN s.weeks_seen<=260 THEN '2-5 years'
                    ELSE '5+ years' END AS duration_bucket
        FROM summary s JOIN latest_occ l ON l.tsr_master_id=s.tsr_master_id AND l.rn=1
        ORDER BY s.weeks_seen DESC, s.tsr_master_id
    """))
    write_csv(ANALYTICS_DIR / "pta_tsr_data_quality_summary.csv", query_rows(conn, """
        SELECT 'Processed PDFs' AS metric, COUNT(*) AS value, 'Source PDFs successfully processed' AS notes FROM source_pdf WHERE status='processed'
        UNION ALL SELECT 'Missing PDFs', COUNT(*), 'PTA API listed item but URL unavailable' FROM source_pdf WHERE status='missing'
        UNION ALL SELECT 'Failed PDFs', COUNT(*), 'Extraction/download failures' FROM source_pdf WHERE status='failed'
        UNION ALL SELECT 'Accepted TSR rows', COUNT(*), 'Validated rows in tsr_occurrence' FROM tsr_occurrence
        UNION ALL SELECT 'Manual-review rows', COUNT(*), 'Rows excluded pending manual review' FROM tsr_rejected_row WHERE rejection_category='manual_review_required'
        UNION ALL SELECT 'Parser artefact rows skipped', COALESCE(SUM(artifact_row_count),0), 'Blank or low-content parser artefacts skipped before review' FROM source_pdf
        UNION ALL SELECT 'Current active TSR rows', COUNT(*), 'Accepted rows in latest processed notice' FROM tsr_occurrence WHERE notice_date=(SELECT MAX(notice_date) FROM source_pdf WHERE status='processed')
    """))


def build_rejection_summary_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for metric, sql in {
        "source_pdfs_total": "SELECT COUNT(*) FROM source_pdf",
        "source_pdfs_processed": "SELECT COUNT(*) FROM source_pdf WHERE status='processed'",
        "accepted_rows_total": "SELECT COUNT(*) FROM tsr_occurrence",
        "manual_review_rows_total": "SELECT COUNT(*) FROM tsr_rejected_row WHERE rejection_category='manual_review_required'",
        "artifact_rows_skipped_total": "SELECT COALESCE(SUM(artifact_row_count),0) FROM source_pdf",
        "latest_processed_notice_date": "SELECT MAX(notice_date) FROM source_pdf WHERE status='processed'",
    }.items():
        rows.append({"section": "overall", "metric": metric, "value": conn.execute(sql).fetchone()[0], "reject_reason": "", "first_notice_date": "", "last_notice_date": "", "accepted_rows": "", "rejected_rows": "", "artifact_rows": "", "rejected_ratio": "", "notes": ""})
    for r in conn.execute("""
        SELECT reject_reason, rejection_category, COUNT(*) AS rejected_rows, MIN(notice_date) AS first_notice_date, MAX(notice_date) AS last_notice_date
        FROM tsr_rejected_row GROUP BY reject_reason, rejection_category ORDER BY rejected_rows DESC
    """):
        rows.append({"section": "reject_reason", "metric": r["rejection_category"], "value": r["rejected_rows"], "reject_reason": r["reject_reason"], "first_notice_date": r["first_notice_date"], "last_notice_date": r["last_notice_date"], "accepted_rows": "", "rejected_rows": r["rejected_rows"], "artifact_rows": "", "rejected_ratio": "", "notes": ""})
    for r in conn.execute("""
        SELECT source_pdf_id, notice_date, filename, status, COALESCE(tsr_row_count,0) accepted_rows,
               COALESCE(rejected_row_count,0) rejected_rows, COALESCE(artifact_row_count,0) artifact_rows,
               CASE WHEN COALESCE(tsr_row_count,0)+COALESCE(rejected_row_count,0)=0 THEN NULL ELSE ROUND(CAST(rejected_row_count AS REAL)/(CAST(tsr_row_count AS REAL)+CAST(rejected_row_count AS REAL)),3) END rejected_ratio,
               extraction_strategy
        FROM source_pdf
        WHERE COALESCE(rejected_row_count,0)>0 OR COALESCE(artifact_row_count,0)>0 OR COALESCE(tsr_row_count,0)=0
        ORDER BY rejected_ratio DESC, artifact_rows DESC, rejected_rows DESC, notice_date
        LIMIT 300
    """):
        rows.append({"section": "pdf_quality_top_300", "metric": r["filename"], "value": r["rejected_ratio"], "reject_reason": "", "first_notice_date": r["notice_date"], "last_notice_date": r["notice_date"], "accepted_rows": r["accepted_rows"], "rejected_rows": r["rejected_rows"], "artifact_rows": r["artifact_rows"], "rejected_ratio": r["rejected_ratio"], "notes": f"source_pdf_id={r['source_pdf_id']}; status={r['status']}; strategy={r['extraction_strategy']}"})
    return rows


def rejection_diagnostics(conn: sqlite3.Connection) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = DIAGNOSTICS_DIR / f"rejection_review_compact_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "01_rejection_summary.csv", build_rejection_summary_rows(conn))
    samples = query_rows(conn, """
        WITH ranked AS (
            SELECT r.rejected_row_id, r.notice_date, s.source_pdf_id, s.filename AS source_pdf, r.source_page, r.source_row_number,
                   r.reject_reason, r.rejection_category, r.suggested_action, r.raw_row_json,
                   ROW_NUMBER() OVER (PARTITION BY r.reject_reason ORDER BY r.notice_date, s.filename, r.source_page, r.source_row_number) rn
            FROM tsr_rejected_row r JOIN source_pdf s ON s.source_pdf_id=r.source_pdf_id
            WHERE r.rejection_category='manual_review_required'
        ) SELECT * FROM ranked WHERE rn<=120 ORDER BY reject_reason, notice_date, source_pdf
    """)
    write_csv(out / "02_rejection_samples.csv", samples)
    export_manual_review_template(conn, out / "03_manual_review_template.csv", instructions=True)
    write_diagnostics_instructions(out)
    return out


def export_manual_review_template(conn: sqlite3.Connection, path: Path, instructions: bool = True) -> None:
    rows: list[dict[str, Any]] = []
    for r in conn.execute("""
        SELECT r.rejected_row_id, r.notice_date, s.source_pdf_id, s.filename AS source_pdf, r.source_page, r.source_row_number,
               r.reject_reason, r.suggested_action, r.raw_row_json
        FROM tsr_rejected_row r JOIN source_pdf s ON s.source_pdf_id=r.source_pdf_id
        WHERE r.rejection_category='manual_review_required' AND COALESCE(r.review_status,'unreviewed')='unreviewed'
        ORDER BY r.notice_date, s.filename, r.source_page, r.source_row_number
        LIMIT 1000
    """):
        cells = []
        try:
            cells = json.loads(r["raw_row_json"] or "[]")
        except Exception:
            pass
        rows.append({
            "rejected_row_id": r["rejected_row_id"], "notice_date": r["notice_date"], "source_pdf_id": r["source_pdf_id"], "source_pdf": r["source_pdf"],
            "source_page": r["source_page"], "source_row_number": r["source_row_number"], "reject_reason": r["reject_reason"], "suggested_action": r["suggested_action"],
            "review_instruction": "Set accept_row=1 and complete corrected_* fields for a valid TSR; set accept_row=0 to permanently ignore; leave blank if unsure.",
            "cell_preview": " | ".join(clean(c) for c in cells),
            "accept_row": "", "corrected_location": "", "corrected_distance_km": "", "corrected_stn_no": "", "corrected_max_speed": "",
            "corrected_date_imposed": "", "corrected_reason": "", "corrected_date_cancelled": "", "corrected_line_key": "", "corrected_line_name": "", "corrected_location_direction": "",
            "review_notes": "", "raw_row_json": r["raw_row_json"],
        })
    write_csv(path, rows)
    if instructions:
        write_review_instructions(path.with_suffix(".txt"), path.name)


def write_review_instructions(path: Path, csv_name: str) -> None:
    path.write_text(f"""PTA TSR manual review instructions
==================================

File to edit:
  {csv_name}

Purpose:
  This CSV contains only rejected rows that the script considers potentially reviewable.
  Blank parser artefacts and low-content fragments are intentionally excluded.

How to review each row:
  1. Open the CSV in Excel or another spreadsheet editor.
  2. Read cell_preview and raw_row_json to understand the extracted row.
  3. If the row is a valid TSR, set accept_row to 1 and complete the corrected_* fields.
  4. If the row is not a valid TSR and should be ignored in future, set accept_row to 0.
  5. If unsure, leave accept_row blank and optionally add review_notes.

Required fields when accept_row=1:
  - corrected_location
  - corrected_max_speed
  - corrected_reason

Recommended fields when available:
  - corrected_distance_km
  - corrected_stn_no
  - corrected_date_imposed
  - corrected_date_cancelled
  - corrected_line_key
  - corrected_line_name
  - corrected_location_direction

Date guidance:
  You can enter dates as the PDF shows them, for example 27 July 2021 or 2021-07-27.
  The script will normalise dates to YYYY-MM-DD during reprocessing where possible.

Speed guidance:
  Enter speeds as 30km/h, 40km/h, 80km/h, etc. The script also accepts variants and normalises them.

How to reprocess after editing:
  Save the edited CSV, then run:

    py .\\pta_tsr_collector.py apply-review --review-file "{csv_name}"

  If the edited file is in another folder, provide the full path, for example:

    py .\\pta_tsr_collector.py apply-review --review-file ".\\pta_tsr_data\\review\\manual_rejection_review_template.csv"

After apply-review:
  Run:

    py .\\pta_tsr_collector.py export

  The accepted manual corrections will then be included in the Power BI CSV outputs.
""", encoding="utf-8")


def write_diagnostics_instructions(out: Path) -> None:
    (out / "README.txt").write_text("""PTA TSR compact diagnostics
===========================

Upload these three CSV files to Copilot when asking for review:
  01_rejection_summary.csv
  02_rejection_samples.csv
  03_manual_review_template.csv

03_manual_review_template.csv has an associated instruction file:
  03_manual_review_template.txt

The instruction file is for local user action and explains how to edit the review CSV and apply reviewed rows back into the dataset.
""", encoding="utf-8")


def apply_review(conn: sqlite3.Connection, review_file: Path) -> None:
    if not review_file.exists():
        raise PipelineError(f"Review file not found: {review_file}")
    applied = ignored = skipped = 0
    with review_file.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for rec in reader:
            decision = clean(rec.get("accept_row", "")).lower()
            rejected_id = clean(rec.get("rejected_row_id", ""))
            if not rejected_id:
                skipped += 1
                continue
            src = conn.execute("SELECT * FROM tsr_rejected_row WHERE rejected_row_id=?", (rejected_id,)).fetchone()
            if not src:
                skipped += 1
                continue
            if decision in {"0", "n", "no", "ignore", "false"}:
                conn.execute("UPDATE tsr_rejected_row SET review_status='ignored' WHERE rejected_row_id=?", (rejected_id,))
                ignored += 1
                continue
            if decision not in {"1", "y", "yes", "true"}:
                skipped += 1
                continue
            fields = {
                "location": clean(rec.get("corrected_location")),
                "stn_no": clean(rec.get("corrected_stn_no")),
                "max_speed": clean(rec.get("corrected_max_speed")),
                "date_imposed": clean(rec.get("corrected_date_imposed")),
                "reason": clean(rec.get("corrected_reason")),
                "date_cancelled": clean(rec.get("corrected_date_cancelled")),
            }
            if not fields["location"] or not fields["max_speed"] or not fields["reason"]:
                skipped += 1
                continue
            cand = CandidateRow([fields["location"], fields["stn_no"], fields["max_speed"], fields["date_imposed"], fields["reason"], fields["date_cancelled"]], int(src["source_page"] or 0), int(src["source_row_number"] or 0), "manual_review", json.dumps(fields, ensure_ascii=False))
            row, reject, artifact = fields_to_row(src["notice_date"], fields, cand)
            if row:
                # Honour explicit line corrections if supplied.
                object.__setattr__(row, "line_key", clean(rec.get("corrected_line_key")) or row.line_key)  # type: ignore[misc]
                object.__setattr__(row, "line_name", clean(rec.get("corrected_line_name")) or row.line_name)  # type: ignore[misc]
                object.__setattr__(row, "location_direction", clean(rec.get("corrected_location_direction")) or row.location_direction)  # type: ignore[misc]
                insert_occurrence(conn, int(src["source_pdf_id"]), row)
                conn.execute("UPDATE tsr_rejected_row SET review_status='accepted' WHERE rejected_row_id=?", (rejected_id,))
                applied += 1
            else:
                skipped += 1
    conn.commit()
    print(f"Applied manual review rows: {applied}")
    print(f"Ignored manual review rows: {ignored}")
    print(f"Skipped/no-action rows: {skipped}")


def print_status(conn: sqlite3.Connection) -> None:
    print("\nStatus summary")
    print("==============")
    for row in conn.execute("SELECT status, COUNT(*) AS count FROM source_pdf GROUP BY status ORDER BY status"):
        print(f"{row['status']}: {row['count']}")
    totals = conn.execute("""
        SELECT (SELECT COUNT(*) FROM source_pdf) AS pdfs,
               (SELECT COUNT(*) FROM tsr_master) AS masters,
               (SELECT COUNT(*) FROM tsr_occurrence) AS occurrences,
               (SELECT COUNT(*) FROM tsr_rejected_row WHERE rejection_category='manual_review_required') AS review_rows,
               (SELECT COALESCE(SUM(artifact_row_count),0) FROM source_pdf) AS artifacts
    """).fetchone()
    print(f"PDFs: {totals['pdfs']}")
    print(f"TSR masters: {totals['masters']}")
    print(f"TSR occurrences: {totals['occurrences']}")
    print(f"Manual review rows: {totals['review_rows']}")
    print(f"Parser artefacts skipped: {totals['artifacts']}")
    latest = latest_notice_date(conn)
    if latest:
        latest_count = conn.execute("SELECT COUNT(*) AS c FROM tsr_occurrence WHERE notice_date=?", (latest,)).fetchone()["c"]
        print(f"Latest processed notice: {latest}")
        print(f"Latest accepted TSR rows: {latest_count}")


def create_diagnostics_folder() -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = DATA_DIR / f"diagnostics_upload_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    for source, label in ((DB_PATH, "database"), (LOG_PATH, "log")):
        if source.exists():
            shutil.copy2(source, out / f"{label}__{source.name}")
    for folder, label in ((EXPORT_DIR, "export"), (ANALYTICS_DIR, "analytics"), (DIAGNOSTICS_DIR, "diagnostics"), (REVIEW_DIR, "review")):
        if not folder.exists():
            continue
        for file in sorted(list(folder.glob("*.csv")) + list(folder.glob("*.json")) + list(folder.glob("*.txt"))):
            shutil.copy2(file, out / f"{label}__{file.name}")
    (out / "MANIFEST.txt").write_text(f"Created: {dt.datetime.now().isoformat(timespec='seconds')}\n", encoding="utf-8")
    return out


def run_discovery(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    found: dict[str, DiscoveredPdf] = {}
    if not args.no_dnn:
        for seed_url in args.seed_url or [DEFAULT_SEED_URL]:
            for pdf in discover_dnn_pdfs(seed_url, args.folder_id or DEFAULT_DNN_FOLDER_IDS, args.module_id, args.tab_id):
                found[pdf.url] = pdf
    registered = register_pdfs(conn, found.values())
    logging.info("Discovery complete. Registered %s PDF links.", registered)


def run_processing(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    statuses = ["discovered", "downloaded"]
    if args.retry_failed:
        statuses.append("failed")
    if args.retry_missing:
        statuses.append("missing")
    if args.force:
        sources = conn.execute("SELECT * FROM source_pdf ORDER BY notice_date, filename").fetchall()
    else:
        placeholders = ",".join("?" for _ in statuses)
        sources = conn.execute(f"SELECT * FROM source_pdf WHERE status IN ({placeholders}) ORDER BY notice_date, filename", tuple(statuses)).fetchall()
    if args.limit:
        sources = sources[:args.limit]
    logging.info("Processing %s PDF(s).", len(sources))
    for source in sources:
        process_source_pdf(conn, source, force=args.force)


def clean_rebuild(_: argparse.Namespace) -> None:
    if DB_PATH.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = DB_PATH.with_suffix(f".sqlite3.bak_{stamp}")
        DB_PATH.rename(backup)
        print(f"Backed up existing database to: {backup}")
    for folder in (EXPORT_DIR, ANALYTICS_DIR, REVIEW_DIR):
        if folder.exists():
            for file in folder.glob("*.csv"):
                file.unlink()
            for file in folder.glob("*.txt"):
                file.unlink()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"PTA Weekly Notices TSR extraction pipeline v{APP_VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(r"""
        Common commands:
          py .\pta_tsr_collector.py clean-rebuild
          py .\pta_tsr_collector.py run --force --install-deps
          py .\pta_tsr_collector.py rejection-diagnostics
          py .\pta_tsr_collector.py export-review-template
          py .\pta_tsr_collector.py apply-review --review-file .\pta_tsr_data\review\manual_rejection_review_template.csv
          py .\pta_tsr_collector.py export
        """),
    )
    parser.add_argument("command", choices=["init", "discover", "process", "run", "export", "status", "diagnostics", "clean-rebuild", "rejection-diagnostics", "export-review-template", "apply-review"])
    parser.add_argument("--seed-url", action="append", default=[])
    parser.add_argument("--folder-id", action="append", type=int, default=[])
    parser.add_argument("--module-id", default=DEFAULT_DNN_MODULE_ID)
    parser.add_argument("--tab-id", default=DEFAULT_DNN_TAB_ID)
    parser.add_argument("--no-dnn", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--retry-missing", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--install-deps", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--review-file", type=Path, default=REVIEW_DIR / "manual_rejection_review_template.csv")
    args = parser.parse_args(argv)
    try:
        ensure_dependencies(args.install_deps)
        import_runtime_dependencies()
        setup_dirs_and_logging(args.verbose)
        if args.command == "clean-rebuild":
            clean_rebuild(args)
            return 0
        conn = connect_db()
        init_db(conn)
        if args.command == "init":
            return 0
        if args.command in {"discover", "run"}:
            run_discovery(args, conn)
            if args.command == "discover":
                print_status(conn)
                return 0
        if args.command in {"process", "run"}:
            run_processing(args, conn)
            export_csvs(conn)
            print_status(conn)
            return 0
        if args.command == "export":
            export_csvs(conn)
            return 0
        if args.command == "status":
            print_status(conn)
            return 0
        if args.command == "diagnostics":
            print(create_diagnostics_folder())
            return 0
        if args.command == "rejection-diagnostics":
            print(rejection_diagnostics(conn))
            return 0
        if args.command == "export-review-template":
            path = REVIEW_DIR / "manual_rejection_review_template.csv"
            export_manual_review_template(conn, path, instructions=True)
            print(path)
            print(path.with_suffix(".txt"))
            return 0
        if args.command == "apply-review":
            apply_review(conn, args.review_file)
            return 0
        return 0
    except KeyboardInterrupt:
        print("\nCancelled by user.")
        return 130
    except Exception as exc:
        print(f"\nERROR: {exc}")
        print(f"Log file: {LOG_PATH}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
