#!/usr/bin/env python3
"""
PTA TSR Collector v2.4.1
========================

Single-file PTA Weekly Notice collector, extractor, normaliser and Power BI
analytics exporter.

v2.4.1 hard requirements:
- no separate analytics script;
- validated TSR row extraction only;
- rejected/quarantined row export;
- normalised ISO date columns;
- line, line name, direction, km range, cause group and lifecycle fields;
- active/resolved lifecycle based on latest processed notice;
- current active exports, line/cause summaries and notice snapshot summary;
- hard export gates so stale v2.3 rows cannot produce misleading Power BI data.
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
APP_VERSION = "2.4.1"
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
    ("Joondalup Line", "JTM", ["leederville", "glendalough", "stirling", "warwick", "whitfords", "edgewater", "joondalup", "nowergup"]),
    ("Fremantle Line", "FTM", ["fremantle", "robbs jetty", "shenton park", "subiaco", "showgrounds", "claremont", "cottesloe", "mciver", "claisebrook"]),
    ("Midland Line", "MTM", ["midland", "bassendean", "success hill", "east guildford", "guildford", "bayswater", "maylands"]),
    ("Armadale Line", "ATM", ["armadale", "kelmscott", "gosnells", "maddington", "kenwick", "beckenham", "oats st", "cannington"]),
    ("Mandurah Line", "RTM", ["mandurah", "rockingham", "warnbro", "kwinana", "wellard", "cockburn", "glen iris", "aubin grove"]),
    ("Thornlie Line", "TTM", ["thornlie"]),
]

BAD_REASON_RE = re.compile(
    r"^(?:TBA|TBC|TBD|N/A|INDEFINITE|PERMANENT TSR|STN NO\.?|UP MAIN|DOWN MAIN|DIRECTION|UP DIRECTION|DOWN DIRECTION|\d{2}-\d{2}-\d{3}|\d{1,2}/\d{1,2}/\d{2,4}|\d+(?:\.\d+)?\s*KM.*)$",
    re.I,
)


class PipelineError(Exception):
    """Expected operational error."""


class MissingPdfError(PipelineError):
    """Raised when a listed PDF cannot be downloaded."""


@dataclass(frozen=True)
class DiscoveredPdf:
    url: str
    filename: str
    notice_date: Optional[str]


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
    row_quality: str
    normalisation_notes: str
    raw_row_json: str


@dataclass(frozen=True)
class RejectedRow:
    notice_date: str
    source_page: int
    source_row_number: int
    reject_reason: str
    raw_row_json: str


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
    for path in (DATA_DIR, PDF_DIR, EXPORT_DIR, ANALYTICS_DIR, LOG_DIR, DIAGNOSTICS_DIR):
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
    return re.sub(r"\s+", " ", str(value).replace("\u2013", "-").replace("\u2014", "-")).strip()


def norm_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9.]+", "", clean(value).upper())


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%d %B %y", "%d %b %y"):
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


def km_range(text: str) -> tuple[Optional[float], Optional[float]]:
    values = [float(v) for v in re.findall(r"(\d+(?:\.\d+)?)\s*km", text, flags=re.I)]
    if not values:
        values = [float(v) for v in re.findall(r"\b(\d{1,3}\.\d{2,4})\b", text)]
    if len(values) >= 2:
        return min(values[0], values[1]), max(values[0], values[1])
    if len(values) == 1:
        return values[0], values[0]
    return None, None


def infer_direction(*parts: object) -> str:
    text = " ".join(clean(p).lower() for p in parts if clean(p))
    if re.search(r"\b(bi|bidi|bi-directional|bidirectional|b-directional|up\s*&\s*down)\b", text):
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
    # Explicit PTA line code first.
    m = re.search(r"\b([A-Z]{2,6})(UP|DN|DM|BI|UM)?\b", upper)
    if m:
        prefix = m.group(1)
        suffix = m.group(2) or {"Up": "UP", "Down": "DN", "Bidirectional": "BI"}.get(direction, "")
        for known_prefix, line_name in LINE_PREFIXES.items():
            if prefix.startswith(known_prefix):
                return f"{known_prefix}{suffix}" if suffix else known_prefix, line_name
    lower = text.lower()
    for line_name, prefix, aliases in LINE_ALIASES:
        if any(alias in lower for alias in aliases):
            suffix = {"Up": "UP", "Down": "DN", "Bidirectional": "BI"}.get(direction, "")
            return f"{prefix}{suffix}" if suffix else prefix, line_name
    return "UNCLASSIFIED", "Unclassified"


def reason_group(reason: str) -> str:
    text = clean(reason).lower()
    if not text or BAD_REASON_RE.match(text):
        return "Invalid / shifted column"
    groups = [
        ("Signals / train control", ["signal", "train control", "interlocking"]),
        ("Rail internal defect", ["ultrasonic", "internal"]),
        ("Rail surface defect", ["rail surface", "surface defect"]),
        ("Track geometry", ["track geometry", "geometry", "alignment"]),
        ("Turnout / points", ["turnout", "points", "switch", "wing rail"]),
        ("Structures / bridge", ["structural", "integrity", "bridge", "structure", "culvert"]),
        ("Level crossing / pedestrian crossing", ["pedestrian", "crossing"]),
        ("Noise mitigation", ["noise"]),
        ("Ballast / formation", ["ballast", "formation", "earthworks"]),
        ("New rail / weld", ["new rail", "weld"]),
        ("Track condition", ["track", "rail", "sleeper", "curve wear", "rcf", "defect", "heat kick"]),
        ("Works / project", ["works", "project", "construction", "scope"]),
        ("Platform / station", ["platform", "station"]),
    ]
    for label, needles in groups:
        if any(n in text for n in needles):
            return label
    return "Other / review required"


def split_location_distance(location_text: str) -> tuple[str, str]:
    text = clean(location_text)
    m = re.search(r"\d+(?:\.\d+)?\s*km", text, flags=re.I)
    if not m:
        return text, ""
    loc = text[:m.start()].strip(" -")
    dist = text[m.start():].strip()
    return loc or text, dist


def validate_reason(reason: str) -> bool:
    return bool(clean(reason)) and not BAD_REASON_RE.match(clean(reason))


def make_row(notice_date: str, cells: list[str], page: int, src_row_num: int) -> tuple[Optional[ExtractedRow], Optional[RejectedRow]]:
    raw = [clean(c) for c in cells]
    if len(raw) < 6:
        return None, RejectedRow(notice_date, page, src_row_num, "too_few_columns", json.dumps(raw, ensure_ascii=False))
    loc_cell, stn_no, speed, imposed_raw, reason_raw, cancelled_raw = raw[:6]
    if not re.search(r"\d+\s*km/h", speed, flags=re.I):
        return None, RejectedRow(notice_date, page, src_row_num, "missing_or_invalid_speed", json.dumps(raw, ensure_ascii=False))
    if not validate_reason(reason_raw):
        return None, RejectedRow(notice_date, page, src_row_num, "invalid_reason_or_shifted_columns", json.dumps(raw, ensure_ascii=False))
    if not re.search(r"\d+(?:\.\d+)?\s*km", loc_cell, flags=re.I):
        return None, RejectedRow(notice_date, page, src_row_num, "missing_km_location", json.dumps(raw, ensure_ascii=False))

    location, distance = split_location_distance(loc_cell)
    direction = infer_direction(loc_cell, distance)
    line_key, line_name = infer_line(loc_cell, location, distance, direction=direction)
    from_km, to_km = km_range(loc_cell)
    year = int(notice_date[:4]) if notice_date else None
    imposed_norm = parse_date(imposed_raw, year)
    cancelled_norm = parse_date(cancelled_raw, year)
    group = reason_group(reason_raw)
    notes = []
    if line_key == "UNCLASSIFIED":
        notes.append("line_not_inferred")
    if direction == "Unknown":
        notes.append("direction_not_inferred")
    if imposed_raw and not imposed_norm:
        notes.append("date_imposed_not_normalised")
    if group in {"Invalid / shifted column", "Other / review required"}:
        notes.append("reason_group_requires_review")
    return ExtractedRow(
        notice_date=notice_date,
        location=location,
        line_section=location,
        distance_km=distance,
        stn_no=stn_no,
        max_speed=speed,
        date_imposed_raw=imposed_raw,
        date_imposed_normalised=imposed_norm,
        reason_raw=reason_raw,
        reason_group=group,
        date_cancelled_raw=cancelled_raw,
        date_cancelled_normalised=cancelled_norm,
        line_key=line_key,
        line_name=line_name,
        location_direction=direction,
        affected_area=clean(f"{location} {distance}"),
        location_from_km=from_km,
        location_to_km=to_km,
        source_page=page,
        source_row_number=src_row_num,
        row_quality="valid" if not notes else "valid_with_notes",
        normalisation_notes="; ".join(notes),
        raw_row_json=json.dumps(raw, ensure_ascii=False),
    ), None


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
        raw_row_json TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (source_pdf_id) REFERENCES source_pdf(source_pdf_id)
    );
    CREATE INDEX IF NOT EXISTS idx_occurrence_notice ON tsr_occurrence(notice_date);
    CREATE INDEX IF NOT EXISTS idx_occurrence_master ON tsr_occurrence(tsr_master_id);
    CREATE INDEX IF NOT EXISTS idx_source_status ON source_pdf(status);
    """)
    # Migrate older databases but block analytics until rows are reprocessed.
    for col, spec in {
        "date_imposed_normalised": "TEXT", "reason_group": "TEXT", "date_cancelled_normalised": "TEXT",
        "line_key": "TEXT", "line_name": "TEXT", "location_direction": "TEXT", "affected_area": "TEXT",
        "location_from_km": "REAL", "location_to_km": "REAL", "row_quality": "TEXT", "normalisation_notes": "TEXT",
    }.items():
        ensure_column(conn, "tsr_occurrence", col, spec)
    ensure_column(conn, "source_pdf", "rejected_row_count", "INTEGER DEFAULT 0")
    conn.commit()


def is_pta_url(url: str) -> bool:
    return urlparse(url).netloc.lower().endswith(PTA_HOST_SUFFIX)


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
    logging.info("DNN session ready moduleId=%s tabId=%s rootFolderId=%s", session.headers.get("moduleid"), session.headers.get("tabid"), cfg.get("root_folder_id", "not-found"))
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
        folders = pdfs = 0
        for item in data.get("Items", []):
            if not isinstance(item, dict):
                continue
            if item.get("IsFolder"):
                try:
                    queue.append(int(item.get("ItemId")))
                    folders += 1
                except Exception:
                    pass
            else:
                pdf = dnn_item_to_pdf(item)
                if pdf:
                    found[pdf.url] = pdf
                    pdfs += 1
        logging.info("DNN folder %s: child_folders=%s weekly_notice_pdfs=%s total_found=%s", folder_id, folders, pdfs, len(found))
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
        conn.execute("UPDATE source_pdf SET local_path=?, file_sha256=?, downloaded_at=?, status='downloaded', last_error=NULL WHERE source_pdf_id=?", (str(local), sha256(response.content), now_iso(), source["source_pdf_id"]))
        conn.commit()
        return local
    raise MissingPdfError(f"Unable to download PDF. Last error: {last_error}")


def table_cell_text(words: list[dict], left: float, right: float, top: float, bottom: float) -> str:
    selected = []
    for word in words:
        cx = (float(word["x0"]) + float(word["x1"])) / 2
        cy = (float(word["top"]) + float(word["bottom"])) / 2
        if left <= cx < right and top <= cy < bottom:
            selected.append(word)
    selected.sort(key=lambda w: (round(float(w["top"]), 1), float(w["x0"])))
    return clean(" ".join(w.get("text", "") for w in selected))


def extract_current_layout_rows(page: Any, notice_date: str) -> tuple[list[ExtractedRow], list[RejectedRow], int]:
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
    edges.sort(key=lambda e: e[0])
    lines: list[tuple[float, float, float]] = []
    for edge in edges:
        if not lines or abs(edge[0] - lines[-1][0]) > 2:
            lines.append(edge)
    x0, x1 = min(e[1] for e in lines), max(e[2] for e in lines)
    width = x1 - x0
    bounds = [x0, x0 + width * .295, x0 + width * .420, x0 + width * .530, x0 + width * .655, x0 + width * .862, x1 + 1]
    rows: list[ExtractedRow] = []
    rejects: list[RejectedRow] = []
    row_number = 0
    for idx in range(1, len(lines) - 1):
        top, bottom = lines[idx][0], lines[idx + 1][0]
        if bottom - top < 8:
            continue
        cells = [table_cell_text(words, bounds[col], bounds[col + 1], top, bottom) for col in range(6)]
        if not any(cells) or re.search(r"Location\s+To\s+and\s+From|Maximum\s+Speed", " ".join(cells), re.I):
            continue
        row_number += 1
        row, reject = make_row(notice_date, cells, page.page_number, row_number)
        if row:
            rows.append(row)
        elif reject:
            rejects.append(reject)
    return rows, rejects, 1 if rows or rejects else 0


def clean_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()


def extract_legacy_rows(page: Any, notice_date: str) -> tuple[list[ExtractedRow], list[RejectedRow], int]:
    tables = page.extract_tables(table_settings={"vertical_strategy": "lines", "horizontal_strategy": "lines", "snap_tolerance": 3, "join_tolerance": 3, "intersection_tolerance": 5}) or []
    rows: list[ExtractedRow] = []
    rejects: list[RejectedRow] = []
    table_count = 0
    for table in tables:
        cleaned = [[clean(cell) for cell in row] for row in table if row]
        cleaned = [row for row in cleaned if any(row)]
        flat = " ".join(clean_header(cell) for row in cleaned[:3] for cell in row)
        if "speed" not in flat or "restriction" not in flat:
            continue
        table_count += 1
        header_idx = 0
        for index, candidate in enumerate(cleaned[:3]):
            header_text = " ".join(clean_header(cell) for cell in candidate)
            if "speed" in header_text and ("restriction" in header_text or "location" in header_text):
                header_idx = index
                break
        header = [clean_header(cell) for cell in cleaned[header_idx]]

        def idx(*needles: str) -> Optional[int]:
            for needle in needles:
                for col, heading in enumerate(header):
                    if needle in heading:
                        return col
            return None

        loc_i = idx("location", "section")
        dist_i = idx("distance", "between")
        stn_i = idx("stn")
        speed_i = idx("maximum speed", "speed")
        imposed_i = idx("imposed")
        reason_i = idx("reason")
        cancel_i = idx("cancel")
        for source_row_number, raw in enumerate(cleaned[header_idx + 1:], start=1):
            loc = raw[loc_i] if loc_i is not None and loc_i < len(raw) else ""
            dist = raw[dist_i] if dist_i is not None and dist_i < len(raw) else ""
            cells = [
                clean(f"{loc} {dist}"),
                raw[stn_i] if stn_i is not None and stn_i < len(raw) else "",
                raw[speed_i] if speed_i is not None and speed_i < len(raw) else "",
                raw[imposed_i] if imposed_i is not None and imposed_i < len(raw) else "",
                raw[reason_i] if reason_i is not None and reason_i < len(raw) else "",
                raw[cancel_i] if cancel_i is not None and cancel_i < len(raw) else "",
            ]
            row, reject = make_row(notice_date, cells, page.page_number, source_row_number)
            if row:
                rows.append(row)
            elif reject:
                rejects.append(reject)
    return rows, rejects, table_count


def extract_rows_from_pdf(pdf_path: Path, notice_date: str) -> tuple[list[ExtractedRow], list[RejectedRow], int, int]:
    accepted: list[ExtractedRow] = []
    rejected: list[RejectedRow] = []
    table_count = 0
    with pdfplumber.open(str(pdf_path)) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            text = page.extract_text() or ""
            if not re.search(r"Current\s+(?:Temporary\s+)?Speed\s+Restrictions|Date\s+to\s+be\s+Cancelled", text, re.I):
                continue
            rows, rejects, count = extract_current_layout_rows(page, notice_date)
            if rows:
                accepted.extend(rows)
                rejected.extend(rejects)
                table_count += count
                continue
            rows, rejects, count = extract_legacy_rows(page, notice_date)
            accepted.extend(rows)
            rejected.extend(rejects)
            table_count += count
    return accepted, rejected, page_count, table_count


def master_fingerprint(row: ExtractedRow) -> str:
    material = "|".join([
        row.line_key,
        f"{row.location_from_km or ''}",
        f"{row.location_to_km or ''}",
        norm_key(row.stn_no),
        row.date_imposed_normalised or norm_key(row.date_imposed_raw),
    ])
    return hashlib.sha256(material.encode()).hexdigest()


def row_fingerprint(row: ExtractedRow) -> str:
    material = "|".join([
        row.line_key,
        row.affected_area,
        norm_key(row.stn_no),
        norm_key(row.max_speed),
        row.date_imposed_normalised,
        norm_key(row.reason_raw),
        row.date_cancelled_normalised,
        str(row.source_page),
        str(row.source_row_number),
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
        rows, rejects, page_count, table_count = extract_rows_from_pdf(path, notice_date)
        for reject in rejects:
            conn.execute(
                """INSERT INTO tsr_rejected_row (source_pdf_id, notice_date, source_page, source_row_number, reject_reason, raw_row_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (sid, reject.notice_date, reject.source_page, reject.source_row_number, reject.reject_reason, reject.raw_row_json, now_iso()),
            )
        for row in rows:
            mid = upsert_master(conn, row)
            conn.execute(
                """INSERT OR IGNORE INTO tsr_occurrence (
                    tsr_master_id, source_pdf_id, notice_date, location, line_section, distance_km, stn_no, max_speed,
                    date_imposed, date_imposed_normalised, reason, reason_group, date_cancelled, date_cancelled_normalised,
                    line_key, line_name, location_direction, affected_area, location_from_km, location_to_km,
                    source_page, source_row_number, row_fingerprint, raw_row_json, row_quality, normalisation_notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mid, sid, row.notice_date, row.location, row.line_section, row.distance_km, row.stn_no, row.max_speed,
                    row.date_imposed_raw, row.date_imposed_normalised, row.reason_raw, row.reason_group, row.date_cancelled_raw,
                    row.date_cancelled_normalised, row.line_key, row.line_name, row.location_direction, row.affected_area,
                    row.location_from_km, row.location_to_km, row.source_page, row.source_row_number, row_fingerprint(row),
                    row.raw_row_json, row.row_quality, row.normalisation_notes, now_iso(),
                ),
            )
        conn.execute(
            """UPDATE source_pdf
               SET notice_date=COALESCE(?, notice_date), processed_at=?, status='processed', last_error=NULL,
                   page_count=?, tsr_table_count=?, tsr_row_count=?, rejected_row_count=?
               WHERE source_pdf_id=?""",
            (notice_date, now_iso(), page_count, table_count, len(rows), len(rejects), sid),
        )
        conn.commit()
        logging.info("Processed %s: accepted=%s rejected=%s", source["filename"], len(rows), len(rejects))
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


def validate_before_export(conn: sqlite3.Connection) -> None:
    total = conn.execute("SELECT COUNT(*) AS c FROM tsr_occurrence").fetchone()["c"]
    if total == 0:
        return
    missing = conn.execute(
        """SELECT COUNT(*) AS c FROM tsr_occurrence
           WHERE COALESCE(NULLIF(TRIM(line_key), ''), '') = ''
              OR COALESCE(NULLIF(TRIM(line_name), ''), '') = ''
              OR COALESCE(NULLIF(TRIM(location_direction), ''), '') = ''
              OR COALESCE(NULLIF(TRIM(affected_area), ''), '') = ''
              OR COALESCE(NULLIF(TRIM(reason_group), ''), '') = ''"""
    ).fetchone()["c"]
    if missing:
        raise PipelineError(
            f"Refusing to export analytics: {missing} of {total} TSR rows are missing required normalised fields. "
            "This usually means the database still contains stale v2.3 rows. Rename pta_tsr_data\\pta_tsr.sqlite3 "
            "and run `py .\\pta_tsr_collector.py run --force --install-deps` for a clean rebuild."
        )
    invalid_reasons = conn.execute(
        """SELECT COUNT(*) AS c FROM tsr_occurrence
           WHERE UPPER(TRIM(reason)) IN ('TBD','TBC','INDEFINITE','PERMANENT TSR','STN NO.')
              OR reason REGEXP '^[0-9]{2}-[0-9]{2}-[0-9]{3}$'"""
    ).fetchone()["c"] if sqlite_supports_regexp(conn) else 0
    if invalid_reasons:
        raise PipelineError(f"Refusing to export analytics: {invalid_reasons} accepted rows have shifted-column reason values.")
    latest = latest_notice_date(conn)
    if latest:
        active_count = conn.execute("SELECT COUNT(*) AS c FROM tsr_occurrence WHERE notice_date=?", (latest,)).fetchone()["c"]
        if active_count == 0:
            raise PipelineError(f"Latest processed notice {latest} has no accepted TSR rows. Inspect rejected rows before refreshing Power BI.")
        bad_active = conn.execute(
            """SELECT COUNT(*) AS c FROM tsr_occurrence WHERE notice_date=? AND
               (COALESCE(NULLIF(TRIM(line_key), ''), '') = '' OR COALESCE(NULLIF(TRIM(max_speed), ''), '') = '' OR
                COALESCE(NULLIF(TRIM(affected_area), ''), '') = '' OR COALESCE(NULLIF(TRIM(reason_group), ''), '') = '')""",
            (latest,),
        ).fetchone()["c"]
        if bad_active:
            raise PipelineError(f"Latest notice {latest} contains {bad_active} active rows with missing required fields.")


def sqlite_supports_regexp(conn: sqlite3.Connection) -> bool:
    try:
        conn.create_function("REGEXP", 2, lambda pat, val: 1 if val and re.search(pat, str(val)) else 0)
        return True
    except Exception:
        return False


def write_csv(path: Path, rows: list[sqlite3.Row] | list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        if not rows:
            handle.write("")
            return
        first = rows[0]
        fieldnames = list(first.keys()) if hasattr(first, "keys") else list(first)
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows([dict(row) for row in rows])


def query_rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


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
               o.source_page, o.source_row_number, o.row_quality, o.normalisation_notes
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
        SELECT source_pdf_id, notice_date, filename, url, status, tsr_table_count, tsr_row_count, rejected_row_count, last_error
        FROM source_pdf ORDER BY notice_date, filename
    """))
    write_csv(EXPORT_DIR / "pta_tsr_rejected_rows.csv", query_rows(conn, """
        SELECT r.rejected_row_id, r.notice_date, s.filename AS source_pdf, r.source_page, r.source_row_number, r.reject_reason, r.raw_row_json
        FROM tsr_rejected_row r JOIN source_pdf s ON s.source_pdf_id=r.source_pdf_id
        ORDER BY r.notice_date, s.filename, r.source_page, r.source_row_number
    """))
    export_analytics(conn, latest)


def export_analytics(conn: sqlite3.Connection, latest: str) -> None:
    active = query_rows(conn, """
        SELECT notice_date, line_key, line_name, location_direction, affected_area, location, distance_km, location_from_km, location_to_km,
               stn_no, max_speed, date_imposed_normalised, reason AS reason_raw, reason_group, date_cancelled AS date_cancelled_raw,
               date_cancelled_normalised, source_page, source_row_number
        FROM tsr_occurrence WHERE notice_date=? ORDER BY source_page, source_row_number
    """, (latest,))
    write_csv(ANALYTICS_DIR / "pta_tsr_active_current.csv", active)
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
    # Compatibility output name, but with corrected semantics: row counts by notice/month, not current active totals.
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
        UNION ALL SELECT 'Rejected / quarantined rows', COUNT(*), 'Rows excluded from analysis because validation failed' FROM tsr_rejected_row
        UNION ALL SELECT 'Current active TSR rows', COUNT(*), 'Accepted rows in latest processed notice' FROM tsr_occurrence WHERE notice_date=(SELECT MAX(notice_date) FROM source_pdf WHERE status='processed')
    """))


def print_status(conn: sqlite3.Connection) -> None:
    print("\nStatus summary")
    print("==============")
    for row in conn.execute("SELECT status, COUNT(*) AS count FROM source_pdf GROUP BY status ORDER BY status"):
        print(f"{row['status']}: {row['count']}")
    totals = conn.execute("""
        SELECT (SELECT COUNT(*) FROM source_pdf) AS pdfs,
               (SELECT COUNT(*) FROM tsr_master) AS masters,
               (SELECT COUNT(*) FROM tsr_occurrence) AS occurrences,
               (SELECT COUNT(*) FROM tsr_rejected_row) AS rejected
    """).fetchone()
    print(f"PDFs: {totals['pdfs']}")
    print(f"TSR masters: {totals['masters']}")
    print(f"TSR occurrences: {totals['occurrences']}")
    print(f"Rejected rows: {totals['rejected']}")
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
    for folder, label in ((EXPORT_DIR, "export"), (ANALYTICS_DIR, "analytics"), (DIAGNOSTICS_DIR, "diagnostics")):
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


def clean_rebuild(args: argparse.Namespace) -> None:
    if DB_PATH.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = DB_PATH.with_suffix(f".sqlite3.bak_{stamp}")
        DB_PATH.rename(backup)
        print(f"Backed up existing database to: {backup}")
    for folder in (EXPORT_DIR, ANALYTICS_DIR):
        if folder.exists():
            for file in folder.glob("*.csv"):
                file.unlink()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"PTA Weekly Notices TSR extraction pipeline v{APP_VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(r"""
        Common commands:
          py .\pta_tsr_collector.py run --install-deps
          py .\pta_tsr_collector.py clean-rebuild
          py .\pta_tsr_collector.py run --force
          py .\pta_tsr_collector.py export
          py .\pta_tsr_collector.py diagnostics
        """),
    )
    parser.add_argument("command", choices=["init", "discover", "process", "run", "export", "status", "diagnostics", "clean-rebuild"])
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
