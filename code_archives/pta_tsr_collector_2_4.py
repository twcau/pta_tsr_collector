#!/usr/bin/env python3
r"""
PTA TSR Collector v2.4
======================

Single-file collector, cleaner, normaliser and analytics exporter for PTA Weekly
Notice Temporary Speed Restriction (TSR) data.

v2.4 focus:
- integrates analytics CSV generation into the main script;
- validates TSR rows before storing them;
- extracts current-layout TSR tables using PDF geometry when possible;
- adds normalised line, direction, km, reason, date and lifecycle fields;
- treats latest-notice presence as active, and absence from the latest notice as resolved;
- exports rejected/quarantined rows for QA instead of silently polluting the model.
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
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import quote, unquote, urljoin, urlparse, urlsplit, urlunsplit

APP_NAME = "pta_tsr_collector"
PTA_BASE_URL = "https://www.pta.wa.gov.au"
DEFAULT_SEED_URL = f"{PTA_BASE_URL}/about-us/working-with-the-pta/safety-resources"
PTA_HOST_SUFFIX = "pta.wa.gov.au"
DNN_CONTENT_ENDPOINT = f"{PTA_BASE_URL}/API/DocumentViewer/ContentService/GetFolderContent"
DEFAULT_DNN_MODULE_ID = "3195"
DEFAULT_DNN_TAB_ID = "1198"
DEFAULT_DNN_FOLDER_IDS = [5160]

REQUIRED_PACKAGES = {
    "requests": "requests",
    "bs4": "beautifulsoup4",
    "pdfplumber": "pdfplumber",
}

ROOT = Path.cwd()
DATA_DIR = ROOT / "pta_tsr_data"
PDF_DIR = DATA_DIR / "pdfs"
EXPORT_DIR = DATA_DIR / "exports"
ANALYTICS_DIR = DATA_DIR / "analytics"
LOG_DIR = DATA_DIR / "logs"
DIAGNOSTICS_DIR = DATA_DIR / "diagnostics"
DB_PATH = DATA_DIR / "pta_tsr.sqlite3"
LOG_PATH = LOG_DIR / "pta_tsr_collector.log"

LINE_CODE_MAP = {
    "JTM": "Joondalup Line",
    "MTM": "Midland Line",
    "FTM": "Fremantle Line",
    "ATM": "Armadale Line",
    "TTM": "Thornlie Line",
    "RTM": "Rockingham / Mandurah Line",
    "MDM": "Mandurah Line",
    "CTM": "City / Central Area",
    "CP": "City / Central Area",
}

CORRIDOR_MAP = [
    ("Joondalup Line", ["leederville", "glendalough", "stirling", "warwick", "whitfords", "edgewater", "joondalup", "nowergup"]),
    ("Fremantle Line", ["fremantle", "robbs jetty", "shenton park", "subiaco", "showgrounds", "claremont", "cottesloe"]),
    ("Midland Line", ["midland", "bassendean", "success hill", "east guildford", "bayswater", "maylands"]),
    ("Armadale Line", ["armadale", "kelmscott", "gosnells", "maddington", "kenwick", "beckenham", "oats st"]),
    ("Mandurah Line", ["mandurah", "rockingham", "warnbro", "kwinana", "wellard", "cockburn", "glen iris"]),
    ("Thornlie Line", ["thornlie"]),
]

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
class TsrRow:
    notice_date: Optional[str]
    location: str
    line_section: str
    distance_km: str
    stn_no: str
    max_speed: str
    date_imposed: str
    reason: str
    date_cancelled: str
    source_page: int
    source_row_number: int
    raw_row_json: str
    line_key: str
    line_name: str
    location_direction: str
    affected_area: str
    location_from_km: Optional[float]
    location_to_km: Optional[float]
    date_imposed_normalised: str
    date_cancelled_normalised: str
    reason_group: str
    row_quality: str
    normalisation_notes: str

@dataclass(frozen=True)
class RejectedRow:
    notice_date: Optional[str]
    source_page: int
    source_row_number: int
    raw_row_json: str
    reject_reason: str


def ensure_dependencies(auto_install: bool = False) -> None:
    missing = [pkg for mod, pkg in REQUIRED_PACKAGES.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    if not auto_install:
        answer = input(f"Missing packages {missing}. Install now? [Y/n]: ").strip().lower()
        if answer not in {"", "y", "yes"}:
            raise PipelineError(f"Install required packages: {sys.executable} -m pip install {' '.join(missing)}")
    subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])


def import_runtime_dependencies() -> None:
    global requests, BeautifulSoup, pdfplumber
    import requests  # type: ignore
    from bs4 import BeautifulSoup  # type: ignore
    import pdfplumber  # type: ignore


def setup_dirs_and_logging(verbose: bool = False) -> None:
    for path in (DATA_DIR, PDF_DIR, EXPORT_DIR, ANALYTICS_DIR, LOG_DIR, DIAGNOSTICS_DIR):
        path.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
        rejected_row_count INTEGER DEFAULT 0
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
        notice_date TEXT,
        location TEXT,
        line_section TEXT,
        distance_km TEXT,
        stn_no TEXT,
        max_speed TEXT,
        date_imposed TEXT,
        reason TEXT,
        date_cancelled TEXT,
        source_page INTEGER,
        source_row_number INTEGER,
        row_fingerprint TEXT NOT NULL,
        raw_row_json TEXT,
        created_at TEXT NOT NULL,
        line_key TEXT,
        line_name TEXT,
        location_direction TEXT,
        affected_area TEXT,
        location_from_km REAL,
        location_to_km REAL,
        date_imposed_normalised TEXT,
        date_cancelled_normalised TEXT,
        reason_group TEXT,
        row_quality TEXT,
        normalisation_notes TEXT,
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
        raw_row_json TEXT,
        reject_reason TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (source_pdf_id) REFERENCES source_pdf(source_pdf_id)
    );
    CREATE INDEX IF NOT EXISTS idx_source_pdf_status ON source_pdf(status);
    CREATE INDEX IF NOT EXISTS idx_occurrence_notice_date ON tsr_occurrence(notice_date);
    CREATE INDEX IF NOT EXISTS idx_occurrence_master ON tsr_occurrence(tsr_master_id);
    """)
    for col, definition in {
        "line_key": "TEXT", "line_name": "TEXT", "location_direction": "TEXT", "affected_area": "TEXT",
        "location_from_km": "REAL", "location_to_km": "REAL", "date_imposed_normalised": "TEXT",
        "date_cancelled_normalised": "TEXT", "reason_group": "TEXT", "row_quality": "TEXT", "normalisation_notes": "TEXT",
    }.items():
        ensure_column(conn, "tsr_occurrence", col, definition)
    ensure_column(conn, "source_pdf", "rejected_row_count", "INTEGER DEFAULT 0")
    conn.commit()


def normalise_space(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\u2013", "-").replace("\u2014", "-")).strip()


def normalise_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9.]+", "", normalise_space(value).upper())


def parse_loose_date(value: str, notice_year: Optional[int] = None) -> str:
    text = normalise_space(value).strip(" -,.()")
    if not text or text.upper() in {"TBA", "TBC", "TBD", "N/A", "INDEFINITE", "PERMANENT TSR"}:
        return ""
    text = text.replace("Febuary", "February").replace("Sept", "Sep")
    text = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", text, flags=re.I)
    text = re.sub(r"([A-Za-z])(?=\d{4}\b)", r"\1 ", text)
    text = normalise_space(text)
    if notice_year and re.match(r"^\d{1,2}\s+[A-Za-z]{3,9}$", text):
        text = f"{text} {notice_year}"
    patterns = ["%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%d %B %y", "%d %b %y"]
    for pattern in patterns:
        try:
            return dt.datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            pass
    return ""


def extract_notice_date_from_url(url: str) -> Optional[str]:
    filename = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    stem = re.sub(r"\.pdf$", "", filename, flags=re.I)
    for pattern in [r"Week\s+Commencing\s+(.+)$", r"Week\s+Ending\s*(?:Fri\s*)?[- ]*(.+)$"]:
        match = re.search(pattern, stem, flags=re.I)
        if match:
            parsed = parse_loose_date(match.group(1))
            if parsed:
                return parsed
    return None


def safe_filename_from_url(url: str) -> str:
    raw = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "weekly_notice.pdf"
    raw = re.sub(r"[<>:\"/\\|?*]+", "_", raw)
    if not raw.lower().endswith(".pdf"):
        raw += ".pdf"
    return f"{raw[:-4]}__{hashlib.sha1(url.encode('utf-8')).hexdigest()[:10]}.pdf"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_pta_url(url: str) -> bool:
    return urlparse(url).netloc.lower().endswith(PTA_HOST_SUFFIX)


def extract_km_values(text: str) -> tuple[Optional[float], Optional[float]]:
    vals = [float(v) for v in re.findall(r"(\d+(?:\.\d+)?)\s*km", text, flags=re.I)]
    if len(vals) >= 2:
        return min(vals[0], vals[1]), max(vals[0], vals[1])
    if len(vals) == 1:
        return vals[0], vals[0]
    vals = [float(v) for v in re.findall(r"\b(\d{1,3}\.\d{2,4})\b", text)]
    if len(vals) >= 2:
        return min(vals[0], vals[1]), max(vals[0], vals[1])
    return None, None


def infer_direction(*values: object) -> str:
    text = " ".join(normalise_space(v).lower() for v in values if normalise_space(v))
    if re.search(r"\b(bi|bi-directional|bidirectional|b-directional|up\s*&\s*down)\b", text):
        return "Bidirectional"
    if "yard" in text:
        return "Yard"
    if re.search(r"\bdown\b|\bdn\b", text):
        return "Down"
    if re.search(r"\bup\b", text):
        return "Up"
    return "Unknown"


def infer_line(code_or_text: str, direction: str = "Unknown") -> tuple[str, str]:
    text = normalise_space(code_or_text)
    upper = text.upper()
    code_match = re.search(r"\b([A-Z]{2,6})(UP|DN|DM|BI|UM)?\b", upper)
    if code_match:
        prefix = code_match.group(1)
        suffix = code_match.group(2) or ""
        for k, line_name in LINE_CODE_MAP.items():
            if prefix.startswith(k):
                line_key = prefix + suffix
                return line_key, line_name
    lower = text.lower()
    for line_name, needles in CORRIDOR_MAP:
        if any(n in lower for n in needles):
            prefix = next((k for k, v in LINE_CODE_MAP.items() if v == line_name), "UNCLASSIFIED")
            suffix = {"Up": "UP", "Down": "DN", "Bidirectional": "BI"}.get(direction, "")
            return f"{prefix}{suffix}" if prefix != "UNCLASSIFIED" else prefix, line_name
    return "UNCLASSIFIED", "Unclassified"


def normalise_reason(reason: str) -> str:
    text = normalise_space(reason).lower()
    if not text:
        return "Unknown / not stated"
    checks = [
        ("Signals / train control", ["signal", "train control", "interlocking"]),
        ("Rail internal defect", ["ultrasonic", "internal"]),
        ("Rail surface defect", ["rail surface"]),
        ("Track geometry", ["track geometry", "geometry"]),
        ("Turnout / points", ["turnout", "points", "switch"]),
        ("Structures / bridge", ["structural", "integrity", "bridge", "structure", "culvert"]),
        ("Level crossing / pedestrian crossing", ["pedestrian", "crossing"]),
        ("Noise mitigation", ["noise"]),
        ("Track condition", ["track", "rail", "sleeper", "ballast", "formation", "defect", "new rail"]),
        ("Works / project", ["works", "project", "construction", "scope"]),
    ]
    for group, needles in checks:
        if any(n in text for n in needles):
            return group
    return "Other / review required"


def split_location_distance(raw_location: str, fallback_distance: str = "") -> tuple[str, str]:
    raw = normalise_space(raw_location)
    fallback_distance = normalise_space(fallback_distance)
    if fallback_distance:
        return raw, fallback_distance
    match = re.search(r"\d+(?:\.\d+)?\s*km", raw, flags=re.I)
    if match:
        return raw[:match.start()].strip(" -"), raw[match.start():].strip()
    return raw, ""


def make_tsr_row(notice_date: Optional[str], cells: Sequence[str], page: int, row_no: int) -> tuple[Optional[TsrRow], Optional[RejectedRow]]:
    raw = [normalise_space(c) for c in cells]
    if len(raw) >= 6:
        loc_raw, stn_no, max_speed, date_imposed, reason, date_cancelled = raw[:6]
        location, distance = split_location_distance(loc_raw)
        line_section = location
    else:
        return None, RejectedRow(notice_date, page, row_no, json.dumps(raw, ensure_ascii=False), "too_few_columns")
    if not re.search(r"\d+\s*km/h", max_speed, flags=re.I):
        return None, RejectedRow(notice_date, page, row_no, json.dumps(raw, ensure_ascii=False), "missing_or_invalid_speed")
    if not (location or distance):
        return None, RejectedRow(notice_date, page, row_no, json.dumps(raw, ensure_ascii=False), "missing_location_and_distance")
    if not reason or re.match(r"^(TBD|TBC|Indefinite|Permanent TSR|\d{2}-\d{2}-\d{3}|\d{1,2}/\d{1,2}/\d{2,4})$", reason, flags=re.I):
        return None, RejectedRow(notice_date, page, row_no, json.dumps(raw, ensure_ascii=False), "invalid_reason_or_shifted_columns")
    notice_year = int(notice_date[:4]) if notice_date else None
    direction = infer_direction(loc_raw, distance)
    line_key, line_name = infer_line(" ".join([location, line_section, distance]), direction)
    if line_key == "UNCLASSIFIED":
        inferred_key, inferred_name = infer_line(loc_raw, direction)
        line_key, line_name = inferred_key, inferred_name
    from_km, to_km = extract_km_values(" ".join([loc_raw, distance]))
    date_imposed_norm = parse_loose_date(date_imposed, notice_year)
    date_cancelled_norm = parse_loose_date(date_cancelled, notice_year)
    notes = []
    if line_key == "UNCLASSIFIED":
        notes.append("line_not_inferred")
    if direction == "Unknown":
        notes.append("direction_not_inferred")
    if not date_imposed_norm and date_imposed:
        notes.append("date_imposed_not_normalised")
    affected_area = normalise_space(" ".join([location, distance])) or loc_raw
    return TsrRow(
        notice_date=notice_date,
        location=location,
        line_section=line_section,
        distance_km=distance,
        stn_no=stn_no,
        max_speed=max_speed,
        date_imposed=date_imposed,
        reason=reason,
        date_cancelled=date_cancelled,
        source_page=page,
        source_row_number=row_no,
        raw_row_json=json.dumps(raw, ensure_ascii=False),
        line_key=line_key,
        line_name=line_name,
        location_direction=direction,
        affected_area=affected_area,
        location_from_km=from_km,
        location_to_km=to_km,
        date_imposed_normalised=date_imposed_norm,
        date_cancelled_normalised=date_cancelled_norm,
        reason_group=normalise_reason(reason),
        row_quality="valid",
        normalisation_notes="; ".join(notes),
    ), None


def table_cell_text(words: list[dict], left: float, right: float, top: float, bottom: float) -> str:
    selected = []
    for word in words:
        cx = (float(word["x0"]) + float(word["x1"])) / 2
        cy = (float(word["top"]) + float(word["bottom"])) / 2
        if left <= cx < right and top <= cy < bottom:
            selected.append(word)
    selected.sort(key=lambda w: (round(float(w["top"]), 1), float(w["x0"])))
    return normalise_space(" ".join(w.get("text", "") for w in selected))


def extract_current_layout_rows(page: Any, notice_date: Optional[str]) -> tuple[list[TsrRow], list[RejectedRow], int]:
    text = page.extract_text() or ""
    if not re.search(r"Location\s+To\s+and\s+From|Date\s+to\s+be\s+Cancelled", text, re.I):
        return [], [], 0
    words = page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False) or []
    edges = []
    for edge in getattr(page, "edges", []) or []:
        if edge.get("orientation") != "h":
            continue
        x0, x1 = float(edge.get("x0", 0)), float(edge.get("x1", 0))
        top = float(edge.get("top", edge.get("y0", 0)))
        if (x1 - x0) >= page.width * 0.70 and 80 <= top <= page.height - 35:
            edges.append((top, x0, x1))
    if len(edges) < 4:
        return [], [], 0
    edges.sort(key=lambda x: x[0])
    lines = []
    for e in edges:
        if not lines or abs(e[0] - lines[-1][0]) > 2:
            lines.append(e)
    x0, x1 = min(e[1] for e in lines), max(e[2] for e in lines)
    width = x1 - x0
    bounds = [x0, x0+width*.295, x0+width*.420, x0+width*.530, x0+width*.655, x0+width*.862, x1+1]
    rows, rejects = [], []
    data_row_no = 0
    for i in range(1, len(lines)-1):
        top, bottom = lines[i][0], lines[i+1][0]
        if bottom - top < 8:
            continue
        cells = [table_cell_text(words, bounds[c], bounds[c+1], top, bottom) for c in range(6)]
        if not any(cells) or re.search(r"Location\s+To\s+and\s+From|Maximum\s+Speed", " ".join(cells), re.I):
            continue
        data_row_no += 1
        row, reject = make_tsr_row(notice_date, cells, page.page_number, data_row_no)
        if row:
            rows.append(row)
        elif reject:
            rejects.append(reject)
    return rows, rejects, 1 if rows or rejects else 0


def clean_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", normalise_space(value).lower()).strip()


def extract_legacy_rows(page: Any, notice_date: Optional[str]) -> tuple[list[TsrRow], list[RejectedRow], int]:
    rows, rejects, table_count = [], [], 0
    tables = page.extract_tables(table_settings={"vertical_strategy":"lines", "horizontal_strategy":"lines", "snap_tolerance":3, "join_tolerance":3, "intersection_tolerance":5}) or []
    for table in tables:
        cleaned = [[normalise_space(c) for c in r] for r in table if r]
        cleaned = [r for r in cleaned if any(r)]
        flat = " ".join(clean_header(c) for r in cleaned[:3] for c in r)
        if "speed" not in flat or "restriction" not in flat:
            continue
        table_count += 1
        header_idx = 0
        for i, r in enumerate(cleaned[:3]):
            h = " ".join(clean_header(c) for c in r)
            if "speed" in h and ("restriction" in h or "location" in h):
                header_idx = i
                break
        header = [clean_header(c) for c in cleaned[header_idx]]
        def idx(*needles: str) -> Optional[int]:
            for n in needles:
                for j, h in enumerate(header):
                    if n in h:
                        return j
            return None
        loc_i = idx("location", "section")
        dist_i = idx("distance", "between")
        stn_i = idx("stn")
        speed_i = idx("maximum speed", "speed")
        imposed_i = idx("imposed")
        reason_i = idx("reason")
        cancel_i = idx("cancel")
        for n, r in enumerate(cleaned[header_idx+1:], start=1):
            if "speed" in " ".join(clean_header(c) for c in r):
                continue
            loc = r[loc_i] if loc_i is not None and loc_i < len(r) else ""
            dist = r[dist_i] if dist_i is not None and dist_i < len(r) else ""
            loc_combined = normalise_space(" ".join([loc, dist]))
            cells = [
                loc_combined,
                r[stn_i] if stn_i is not None and stn_i < len(r) else "",
                r[speed_i] if speed_i is not None and speed_i < len(r) else "",
                r[imposed_i] if imposed_i is not None and imposed_i < len(r) else "",
                r[reason_i] if reason_i is not None and reason_i < len(r) else "",
                r[cancel_i] if cancel_i is not None and cancel_i < len(r) else "",
            ]
            row, reject = make_tsr_row(notice_date, cells, page.page_number, n)
            if row:
                rows.append(row)
            elif reject:
                rejects.append(reject)
    return rows, rejects, table_count


def extract_rows_from_pdf(pdf_path: Path, notice_date: Optional[str]) -> tuple[list[TsrRow], list[RejectedRow], int, int]:
    rows, rejects, table_count = [], [], 0
    with pdfplumber.open(str(pdf_path)) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            text = page.extract_text() or ""
            if not re.search(r"Current\s+(?:Temporary\s+)?Speed\s+Restrictions|Date\s+to\s+be\s+Cancelled", text, re.I):
                continue
            c_rows, c_rejects, c_tables = extract_current_layout_rows(page, notice_date)
            if c_rows:
                rows.extend(c_rows); rejects.extend(c_rejects); table_count += c_tables
                continue
            l_rows, l_rejects, l_tables = extract_legacy_rows(page, notice_date)
            rows.extend(l_rows); rejects.extend(l_rejects); table_count += l_tables
    return rows, rejects, page_count, table_count


def master_fingerprint(row: TsrRow) -> str:
    material = "|".join([row.line_key, str(row.location_from_km), str(row.location_to_km), normalise_key(row.stn_no), row.date_imposed_normalised or normalise_key(row.date_imposed)])
    return hashlib.sha256(material.encode()).hexdigest()


def row_fingerprint(row: TsrRow) -> str:
    material = "|".join([row.line_key, row.affected_area, row.stn_no, row.max_speed, row.date_imposed_normalised, normalise_key(row.reason), row.date_cancelled_normalised, str(row.source_page), str(row.source_row_number)])
    return hashlib.sha256(material.encode()).hexdigest()


def upsert_master(conn: sqlite3.Connection, row: TsrRow) -> int:
    fp = master_fingerprint(row)
    stamp = now_iso()
    conn.execute("""INSERT INTO tsr_master (master_fingerprint, created_at, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(master_fingerprint) DO UPDATE SET updated_at=excluded.updated_at""", (fp, stamp, stamp))
    return int(conn.execute("SELECT tsr_master_id FROM tsr_master WHERE master_fingerprint=?", (fp,)).fetchone()[0])


def extract_dnn_page_config(html: str) -> dict[str, str]:
    config = {}
    for key, pattern in {"module_id": r'moduleId:\s*"(\d+)"', "root_folder_id": r'rootFolderId:\s*"(\d+)"'}.items():
        m = re.search(pattern, html, re.I)
        if m: config[key] = m.group(1)
    m = re.search(r'name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)["\']', html, re.I)
    if m: config["request_verification_token"] = m.group(1)
    return config


def create_dnn_session(seed_url: str, module_id: str, tab_id: str):
    session = requests.Session()
    session.headers.update({"User-Agent": f"{APP_NAME}/2.4", "Accept": "application/json, text/javascript, */*; q=0.01", "X-Requested-With": "XMLHttpRequest", "Referer": seed_url, "moduleid": str(module_id), "tabid": str(tab_id)})
    response = session.get(seed_url, timeout=45); response.raise_for_status()
    config = extract_dnn_page_config(response.text)
    session.headers.update({"moduleid": config.get("module_id", module_id), "tabid": config.get("tab_id", tab_id)})
    if config.get("request_verification_token"):
        session.headers.update({"requestverificationtoken": config["request_verification_token"]})
    logging.info("DNN config: moduleId=%s tabId=%s rootFolderId=%s token=%s", session.headers.get("moduleid"), session.headers.get("tabid"), config.get("root_folder_id", "not-found"), "found" if config.get("request_verification_token") else "not-found")
    return session


def get_dnn_folder_content(session: Any, folder_id: int) -> dict[str, object]:
    response = session.get(DNN_CONTENT_ENDPOINT, params={"startIndex":0,"numItems":2147483647,"sort":"Name asc","folderId":folder_id}, timeout=60)
    response.raise_for_status()
    data = response.json()
    (DIAGNOSTICS_DIR / f"dnn_folder_{folder_id}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def dnn_item_to_pdf(item: dict[str, object]) -> Optional[DiscoveredPdf]:
    if bool(item.get("IsFolder")): return None
    name, url, ext = normalise_space(item.get("Name")), normalise_space(item.get("Url")), normalise_space(item.get("Extension")).lower().lstrip(".")
    if ext != "pdf" or not url or not re.search(r"weekly\s*notice", name, re.I): return None
    full = urljoin(PTA_BASE_URL, url)
    return DiscoveredPdf(full, safe_filename_from_url(full), extract_notice_date_from_url(full))


def discover_dnn_pdf_links(seed_url: str, folder_ids: list[int], module_id: str, tab_id: str) -> list[DiscoveredPdf]:
    session = create_dnn_session(seed_url, module_id, tab_id)
    queue = list(dict.fromkeys(folder_ids or DEFAULT_DNN_FOLDER_IDS)); seen=set(); found={}
    while queue:
        fid = queue.pop(0)
        if fid in seen: continue
        seen.add(fid)
        data = get_dnn_folder_content(session, fid)
        items = data.get("Items", []) if isinstance(data, dict) else []
        folders = pdfs = 0
        for item in items:
            if not isinstance(item, dict): continue
            if item.get("IsFolder"):
                try: queue.append(int(item.get("ItemId"))); folders += 1
                except Exception: pass
            else:
                pdf = dnn_item_to_pdf(item)
                if pdf: found[pdf.url]=pdf; pdfs += 1
        logging.info("DNN folder %s: child_folders=%s weekly_notice_pdfs=%s total_found=%s", fid, folders, pdfs, len(found))
        time.sleep(.15)
    return sorted(found.values(), key=lambda x: (x.notice_date or "9999-99-99", x.url))


def register_pdfs(conn: sqlite3.Connection, pdfs: Iterable[DiscoveredPdf]) -> int:
    n=0
    for pdf in pdfs:
        conn.execute("""INSERT INTO source_pdf (url, filename, url_hash, notice_date, discovered_at, status)
                        VALUES (?, ?, ?, ?, ?, 'discovered')
                        ON CONFLICT(url) DO UPDATE SET filename=excluded.filename, notice_date=COALESCE(source_pdf.notice_date, excluded.notice_date)""",
                     (pdf.url, pdf.filename, hashlib.sha256(pdf.url.encode()).hexdigest(), pdf.notice_date, now_iso()))
        n+=1
    conn.commit(); return n


def build_download_candidates(url: str) -> list[str]:
    split = urlsplit(url); path = unquote(split.path); variants=[path]
    for old,new in {"Febuary":"February", "Week Ending- ":"Week Ending - ", "Week Ending-":"Week Ending -"}.items():
        if old in path: variants.append(path.replace(old,new))
    out=[]
    for p in variants:
        ep=quote(p, safe="/%")
        out.append(urlunsplit((split.scheme, split.netloc, ep, split.query, split.fragment)))
        out.append(urlunsplit((split.scheme, split.netloc, ep, "", split.fragment)))
    return list(dict.fromkeys(out))


def download_source_pdf(conn: sqlite3.Connection, source: sqlite3.Row, force: bool=False) -> Path:
    local = PDF_DIR / source["filename"]
    if local.exists() and not force: return local
    last=""
    for i,u in enumerate(build_download_candidates(source["url"]), start=1):
        logging.info("Downloading candidate %s: %s", i, u)
        try: r = requests.get(u, timeout=120, allow_redirects=True)
        except Exception as exc: last=str(exc); continue
        if r.status_code == 404: last=f"404 Not Found: {u}"; continue
        try: r.raise_for_status()
        except Exception as exc: last=str(exc); continue
        if not r.content.startswith(b"%PDF"): last=f"Downloaded content is not a PDF from {u}"; continue
        local.write_bytes(r.content)
        conn.execute("UPDATE source_pdf SET local_path=?, file_sha256=?, downloaded_at=?, status='downloaded', last_error=NULL WHERE source_pdf_id=?", (str(local), sha256_bytes(r.content), now_iso(), source["source_pdf_id"]))
        conn.commit(); return local
    raise MissingPdfError(f"Unable to download PDF after candidate URLs. Last error: {last}")


def process_source_pdf(conn: sqlite3.Connection, source: sqlite3.Row, force: bool=False) -> None:
    if source["status"] == "processed" and not force:
        logging.info("Skipping previously processed PDF: %s", source["filename"]); return
    sid = int(source["source_pdf_id"])
    try:
        path = download_source_pdf(conn, source)
        notice_date = source["notice_date"] or extract_notice_date_from_url(source["url"])
        rows, rejects, page_count, table_count = extract_rows_from_pdf(path, notice_date)
        if force:
            conn.execute("DELETE FROM tsr_occurrence WHERE source_pdf_id=?", (sid,)); conn.execute("DELETE FROM tsr_rejected_row WHERE source_pdf_id=?", (sid,))
        for reject in rejects:
            conn.execute("INSERT INTO tsr_rejected_row (source_pdf_id, notice_date, source_page, source_row_number, raw_row_json, reject_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (sid, reject.notice_date, reject.source_page, reject.source_row_number, reject.raw_row_json, reject.reject_reason, now_iso()))
        for row in rows:
            mid = upsert_master(conn, row)
            conn.execute("""INSERT OR IGNORE INTO tsr_occurrence (
                tsr_master_id, source_pdf_id, notice_date, location, line_section, distance_km, stn_no, max_speed, date_imposed, reason, date_cancelled,
                source_page, source_row_number, row_fingerprint, raw_row_json, created_at, line_key, line_name, location_direction, affected_area,
                location_from_km, location_to_km, date_imposed_normalised, date_cancelled_normalised, reason_group, row_quality, normalisation_notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (mid, sid, row.notice_date, row.location, row.line_section, row.distance_km, row.stn_no, row.max_speed, row.date_imposed, row.reason, row.date_cancelled,
                 row.source_page, row.source_row_number, row_fingerprint(row), row.raw_row_json, now_iso(), row.line_key, row.line_name, row.location_direction,
                 row.affected_area, row.location_from_km, row.location_to_km, row.date_imposed_normalised, row.date_cancelled_normalised, row.reason_group, row.row_quality, row.normalisation_notes))
        conn.execute("UPDATE source_pdf SET notice_date=COALESCE(?, notice_date), processed_at=?, status='processed', last_error=NULL, page_count=?, tsr_table_count=?, tsr_row_count=?, rejected_row_count=? WHERE source_pdf_id=?", (notice_date, now_iso(), page_count, table_count, len(rows), len(rejects), sid))
        conn.commit(); logging.info("Processed %s: valid_rows=%s rejected_rows=%s", source["filename"], len(rows), len(rejects))
    except MissingPdfError as exc:
        conn.rollback(); conn.execute("UPDATE source_pdf SET status='missing', last_error=? WHERE source_pdf_id=?", (str(exc), sid)); conn.commit()
    except Exception as exc:
        conn.rollback(); logging.exception("Failed processing %s", source["filename"]); conn.execute("UPDATE source_pdf SET status='failed', last_error=? WHERE source_pdf_id=?", (str(exc), sid)); conn.commit()


def latest_processed_notice_date(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(notice_date) AS d FROM source_pdf WHERE status='processed'").fetchone()
    return row["d"] or ""


def export_query(conn: sqlite3.Connection, filename: str, sql: str) -> None:
    rows = conn.execute(sql).fetchall(); path = EXPORT_DIR / filename
    with path.open("w", newline="", encoding="utf-8-sig") as h:
        if rows:
            w=csv.DictWriter(h, fieldnames=rows[0].keys()); w.writeheader(); w.writerows([dict(r) for r in rows])
        else: h.write("")
    logging.info("Exported %s", path)


def export_csvs(conn: sqlite3.Connection) -> None:
    latest = latest_processed_notice_date(conn)
    export_query(conn, "pta_tsr_occurrences.csv", """
        SELECT o.tsr_record_id, o.tsr_master_id, o.notice_date, o.location, o.line_section, o.distance_km, o.stn_no, o.max_speed,
               o.date_imposed, o.date_imposed_normalised, o.reason, o.reason_group, o.date_cancelled, o.date_cancelled_normalised,
               o.line_key, o.line_name, o.location_direction, o.affected_area, o.location_from_km, o.location_to_km,
               s.filename AS source_pdf, s.url AS source_url, o.source_page, o.source_row_number, o.row_quality, o.normalisation_notes
        FROM tsr_occurrence o JOIN source_pdf s ON s.source_pdf_id=o.source_pdf_id
        ORDER BY o.notice_date, o.tsr_record_id
    """)
    export_query(conn, "pta_tsr_masters.csv", f"""
        WITH latest_occ AS (
            SELECT o.*, ROW_NUMBER() OVER (PARTITION BY tsr_master_id ORDER BY notice_date DESC, tsr_record_id DESC) AS rn
            FROM tsr_occurrence o
        ), summary AS (
            SELECT tsr_master_id, MIN(notice_date) AS first_seen_notice_date, MAX(notice_date) AS last_seen_notice_date, COUNT(*) AS weeks_seen
            FROM tsr_occurrence GROUP BY tsr_master_id
        )
        SELECT s.tsr_master_id, s.first_seen_notice_date, s.last_seen_notice_date, s.weeks_seen,
               CASE WHEN s.last_seen_notice_date='{latest}' THEN 1 ELSE 0 END AS is_active,
               CASE WHEN s.last_seen_notice_date='{latest}' THEN '' ELSE s.last_seen_notice_date END AS resolved_date,
               l.location AS latest_location, l.line_section AS latest_line_section, l.distance_km AS latest_distance_km,
               l.stn_no AS latest_stn_no, l.max_speed AS latest_max_speed,
               l.date_imposed AS date_imposed, l.date_imposed_normalised,
               l.reason AS latest_reason, l.reason_group AS latest_reason_group,
               CASE WHEN s.last_seen_notice_date='{latest}' THEN '' ELSE COALESCE(NULLIF(l.date_cancelled_normalised,''), s.last_seen_notice_date) END AS latest_date_cancelled,
               l.date_cancelled AS latest_date_cancelled_raw,
               l.line_key, l.line_name, l.location_direction, l.affected_area, l.location_from_km, l.location_to_km,
               m.master_fingerprint, l.row_quality, l.normalisation_notes
        FROM summary s JOIN latest_occ l ON l.tsr_master_id=s.tsr_master_id AND l.rn=1
        JOIN tsr_master m ON m.tsr_master_id=s.tsr_master_id
        ORDER BY s.first_seen_notice_date, s.tsr_master_id
    """)
    export_query(conn, "pta_tsr_source_pdfs.csv", """
        SELECT source_pdf_id, notice_date, filename, url, status, tsr_table_count, tsr_row_count, rejected_row_count, last_error
        FROM source_pdf ORDER BY notice_date, filename
    """)
    export_query(conn, "pta_tsr_rejected_rows.csv", """
        SELECT r.rejected_row_id, r.notice_date, s.filename AS source_pdf, r.source_page, r.source_row_number, r.reject_reason, r.raw_row_json
        FROM tsr_rejected_row r JOIN source_pdf s ON s.source_pdf_id=r.source_pdf_id
        ORDER BY r.notice_date, s.filename, r.source_page, r.source_row_number
    """)
    export_analytics(conn)


def write_rows(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as h:
        if rows:
            w=csv.DictWriter(h, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
        else: h.write("")


def export_analytics(conn: sqlite3.Connection) -> None:
    latest = latest_processed_notice_date(conn)
    active = [dict(r) for r in conn.execute("""
        SELECT notice_date, line_key, line_name, location_direction, affected_area, location, distance_km, stn_no, max_speed,
               date_imposed_normalised, reason, reason_group, date_cancelled, source_page, source_row_number
        FROM tsr_occurrence WHERE notice_date=? ORDER BY source_page, source_row_number
    """, (latest,))]
    write_rows(ANALYTICS_DIR / "pta_tsr_active_current.csv", active)
    write_rows(ANALYTICS_DIR / "pta_tsr_active_by_line.csv", [dict(r) for r in conn.execute("""
        SELECT line_key, line_name, location_direction, COUNT(*) AS active_tsr_count
        FROM tsr_occurrence WHERE notice_date=? GROUP BY line_key, line_name, location_direction ORDER BY active_tsr_count DESC
    """, (latest,))])
    write_rows(ANALYTICS_DIR / "pta_tsr_active_by_cause.csv", [dict(r) for r in conn.execute("""
        SELECT reason_group, COUNT(*) AS active_tsr_count
        FROM tsr_occurrence WHERE notice_date=? GROUP BY reason_group ORDER BY active_tsr_count DESC
    """, (latest,))])
    write_rows(ANALYTICS_DIR / "pta_tsr_data_quality_summary.csv", [dict(r) for r in conn.execute("""
        SELECT 'Processed PDFs' AS metric, COUNT(*) AS value, 'Source PDFs successfully processed' AS notes FROM source_pdf WHERE status='processed'
        UNION ALL SELECT 'Missing PDFs', COUNT(*), 'PTA API listed item but URL unavailable' FROM source_pdf WHERE status='missing'
        UNION ALL SELECT 'Total TSR occurrences', COUNT(*), 'Valid TSR rows accepted into tsr_occurrence' FROM tsr_occurrence
        UNION ALL SELECT 'Rejected / quarantined rows', COUNT(*), 'Rows rejected by validation and excluded from analysis' FROM tsr_rejected_row
    """)])
    # Summary files retained for Power BI compatibility.
    write_rows(ANALYTICS_DIR / "pta_tsr_line_month_summary.csv", [dict(r) for r in conn.execute("""
        SELECT substr(notice_date,1,4) AS year, substr(notice_date,6,2) AS month, substr(notice_date,1,7) AS year_month,
               line_key, COUNT(*) AS restriction_occurrences, COUNT(DISTINCT tsr_master_id) AS distinct_tsr_count
        FROM tsr_occurrence GROUP BY year, month, year_month, line_key ORDER BY year_month, line_key
    """)])
    write_rows(ANALYTICS_DIR / "pta_tsr_reason_summary.csv", [dict(r) for r in conn.execute("""
        SELECT reason_group, reason AS reason_raw, COUNT(*) AS restriction_occurrences, COUNT(DISTINCT tsr_master_id) AS distinct_tsr_count
        FROM tsr_occurrence GROUP BY reason_group, reason ORDER BY restriction_occurrences DESC
    """)])
    write_rows(ANALYTICS_DIR / "pta_tsr_segment_summary.csv", [dict(r) for r in conn.execute("""
        SELECT line_key, line_name, location_direction, affected_area, location_from_km, location_to_km,
               COUNT(*) AS restriction_occurrences, COUNT(DISTINCT tsr_master_id) AS distinct_tsr_count,
               MIN(notice_date) AS first_seen_notice_date, MAX(notice_date) AS last_seen_notice_date
        FROM tsr_occurrence GROUP BY line_key, line_name, location_direction, affected_area, location_from_km, location_to_km
        ORDER BY restriction_occurrences DESC
    """)])
    export_query(conn, "../analytics/pta_tsr_duration_buckets.csv", f"""
        SELECT * FROM (
            WITH summary AS (SELECT tsr_master_id, MIN(notice_date) first_seen_notice_date, MAX(notice_date) last_seen_notice_date, COUNT(*) weeks_seen FROM tsr_occurrence GROUP BY tsr_master_id),
            latest_occ AS (SELECT o.*, ROW_NUMBER() OVER (PARTITION BY tsr_master_id ORDER BY notice_date DESC, tsr_record_id DESC) rn FROM tsr_occurrence o)
            SELECT s.tsr_master_id, l.line_key, l.line_name, l.location_direction, l.affected_area, l.reason AS latest_reason, l.reason_group,
                   s.first_seen_notice_date, s.last_seen_notice_date, s.weeks_seen,
                   CASE WHEN s.last_seen_notice_date='{latest}' THEN 1 ELSE 0 END AS is_active,
                   CASE WHEN s.last_seen_notice_date='{latest}' THEN '' ELSE s.last_seen_notice_date END AS resolved_date,
                   CASE WHEN s.weeks_seen<=1 THEN '1 week' WHEN s.weeks_seen<=4 THEN '2-4 weeks' WHEN s.weeks_seen<=13 THEN '1-3 months'
                        WHEN s.weeks_seen<=26 THEN '3-6 months' WHEN s.weeks_seen<=52 THEN '6-12 months' WHEN s.weeks_seen<=104 THEN '1-2 years'
                        WHEN s.weeks_seen<=260 THEN '2-5 years' ELSE '5+ years' END AS duration_bucket
            FROM summary s JOIN latest_occ l ON l.tsr_master_id=s.tsr_master_id AND l.rn=1
        ) ORDER BY weeks_seen DESC
    """)


def print_status(conn: sqlite3.Connection) -> None:
    print("\nStatus summary\n==============")
    for r in conn.execute("SELECT status, COUNT(*) AS count FROM source_pdf GROUP BY status ORDER BY status"):
        print(f"{r['status']}: {r['count']}")
    t=conn.execute("SELECT (SELECT COUNT(*) FROM source_pdf) pdfs,(SELECT COUNT(*) FROM tsr_master) masters,(SELECT COUNT(*) FROM tsr_occurrence) occurrences,(SELECT COUNT(*) FROM tsr_rejected_row) rejected").fetchone()
    print(f"PDFs: {t['pdfs']}\nTSR masters: {t['masters']}\nTSR occurrences: {t['occurrences']}\nRejected rows: {t['rejected']}")


def create_diagnostics_folder() -> Path:
    out=DATA_DIR / f"diagnostics_upload_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"; out.mkdir(parents=True, exist_ok=True)
    for src,label in [(DB_PATH,"database"),(LOG_PATH,"log")]:
        if src.exists(): shutil.copy2(src, out / f"{label}__{src.name}")
    for d,label in [(EXPORT_DIR,"export"),(ANALYTICS_DIR,"analytics"),(DIAGNOSTICS_DIR,"dnn")]:
        if d.exists():
            for f in sorted(list(d.glob("*.csv"))+list(d.glob("*.json"))+list(d.glob("*.txt"))): shutil.copy2(f, out / f"{label}__{f.name}")
    (out/"MANIFEST.txt").write_text(f"Created {dt.datetime.now().isoformat()}\n", encoding="utf-8")
    return out


def run_discovery(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    discovered={}
    if not args.no_dnn:
        for seed in args.seed_url or [DEFAULT_SEED_URL]:
            discovered.update({p.url:p for p in discover_dnn_pdf_links(seed, args.folder_id or DEFAULT_DNN_FOLDER_IDS, args.module_id, args.tab_id)})
    logging.info("Discovery complete. Found/registered %s PDF link(s).", register_pdfs(conn, discovered.values()))


def run_processing(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    statuses=["discovered","downloaded"]
    if args.retry_failed: statuses.append("failed")
    if args.retry_missing: statuses.append("missing")
    if args.force:
        sources=conn.execute("SELECT * FROM source_pdf ORDER BY notice_date, filename").fetchall()
    else:
        sources=conn.execute(f"SELECT * FROM source_pdf WHERE status IN ({','.join('?' for _ in statuses)}) ORDER BY notice_date, filename", tuple(statuses)).fetchall()
    if args.limit: sources=sources[:args.limit]
    for s in sources: process_source_pdf(conn, s, force=args.force)


def main(argv: Optional[list[str]]=None) -> int:
    parser=argparse.ArgumentParser(description="PTA Weekly Notices TSR extraction pipeline v2.4", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=textwrap.dedent(r"""
    Common commands:
      py .\pta_tsr_collector.py run --install-deps
      py .\pta_tsr_collector.py run --force
      py .\pta_tsr_collector.py diagnostics
    """))
    parser.add_argument("command", choices=["init","discover","process","run","export","status","diagnostics"])
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
    args=parser.parse_args(argv)
    try:
        ensure_dependencies(args.install_deps); import_runtime_dependencies(); setup_dirs_and_logging(args.verbose)
        conn=connect_db(); init_db(conn)
        if args.command == "init": return 0
        if args.command in {"discover","run"}: run_discovery(args, conn)
        if args.command in {"process","run"}: run_processing(args, conn); export_csvs(conn); print_status(conn); return 0
        if args.command == "export": export_csvs(conn); return 0
        if args.command == "status": print_status(conn); return 0
        if args.command == "diagnostics": print(create_diagnostics_folder()); return 0
        return 0
    except KeyboardInterrupt:
        print("\nCancelled by user."); return 130
    except Exception as exc:
        print(f"\nERROR: {exc}\nLog file: {LOG_PATH}"); return 1

if __name__ == "__main__":
    raise SystemExit(main())
