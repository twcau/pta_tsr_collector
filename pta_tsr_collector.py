#!/usr/bin/env python3
r"""
PTA TSR Collector v2.3
======================

Collects PTA Weekly Notice PDFs via the PTA DNN Document Viewer API, extracts
Current Temporary Speed Restriction / Current Speed Restriction tables, stores a
resumable SQLite history, assigns TSR master records, and exports CSV files.

Default working folder:
    C:\PythonScripts\pta_tsr_collector

Default script path:
    C:\PythonScripts\pta_tsr_collector\pta_tsr_collector.py
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
LOG_DIR = DATA_DIR / "logs"
DIAGNOSTICS_DIR = DATA_DIR / "diagnostics"
DB_PATH = DATA_DIR / "pta_tsr.sqlite3"
LOG_PATH = LOG_DIR / "pta_tsr_collector.log"


class PipelineError(Exception):
    """Expected pipeline error with a useful user-facing message."""


class MissingPdfError(PipelineError):
    """Raised when a PDF listed by PTA cannot be downloaded."""


@dataclass(frozen=True)
class DiscoveredPdf:
    url: str
    filename: str
    notice_date: Optional[str]


@dataclass(frozen=True)
class ExtractedTsrRow:
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


def ensure_dependencies(auto_install: bool = False) -> None:
    missing = []
    for module_name, package_name in REQUIRED_PACKAGES.items():
        if importlib.util.find_spec(module_name) is None:
            missing.append(package_name)

    if not missing:
        return

    print("\nMissing required Python packages:")
    for package in missing:
        print(f"  - {package}")

    if not auto_install:
        answer = input("\nInstall missing packages now using pip? [Y/n]: ").strip().lower()
        if answer not in {"", "y", "yes"}:
            raise PipelineError(
                "Missing dependencies. Re-run with --install-deps or install manually with: "
                f"{sys.executable} -m pip install {' '.join(missing)}"
            )

    cmd = [sys.executable, "-m", "pip", "install", *missing]
    print("\nRunning:", " ".join(cmd))
    subprocess.check_call(cmd)


def import_runtime_dependencies() -> None:
    global requests, BeautifulSoup, pdfplumber
    import requests  # type: ignore
    from bs4 import BeautifulSoup  # type: ignore
    import pdfplumber  # type: ignore


def setup_dirs_and_logging(verbose: bool = False) -> None:
    for path in (DATA_DIR, PDF_DIR, EXPORT_DIR, LOG_DIR, DIAGNOSTICS_DIR):
        path.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
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
            tsr_row_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS tsr_master (
            tsr_master_id INTEGER PRIMARY KEY AUTOINCREMENT,
            master_fingerprint TEXT NOT NULL UNIQUE,
            location_key TEXT NOT NULL,
            distance_key TEXT NOT NULL,
            stn_key TEXT NOT NULL,
            date_imposed_key TEXT NOT NULL,
            first_seen_notice_date TEXT,
            last_seen_notice_date TEXT,
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
            FOREIGN KEY (tsr_master_id) REFERENCES tsr_master(tsr_master_id),
            FOREIGN KEY (source_pdf_id) REFERENCES source_pdf(source_pdf_id),
            UNIQUE (source_pdf_id, row_fingerprint)
        );

        CREATE INDEX IF NOT EXISTS idx_source_pdf_status ON source_pdf(status);
        CREATE INDEX IF NOT EXISTS idx_occurrence_notice_date ON tsr_occurrence(notice_date);
        CREATE INDEX IF NOT EXISTS idx_occurrence_master ON tsr_occurrence(tsr_master_id);
        """
    )
    conn.commit()


def normalise_space(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", text).strip()


def normalise_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9.]+", "", normalise_space(value).upper())


def parse_loose_date(value: str) -> Optional[str]:
    text = normalise_space(value).strip(" -")
    if not text or text.upper() in {"TBA", "TBC", "TBD", "N/A", "INDEFINITE", "PERMANENT TSR"}:
        return None

    text = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", text, flags=re.I)
    text = text.replace("Febuary", "February").replace("Sept ", "Sep ")
    text = re.sub(r"([A-Za-z])(?=\d{4}\b)", r"\1 ", text)
    text = normalise_space(text)

    patterns = ["%d %B %Y", "%d %b %Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%B %Y", "%b %Y"]
    for pattern in patterns:
        try:
            parsed = dt.datetime.strptime(text, pattern).date()
            if pattern in {"%B %Y", "%b %Y"}:
                parsed = parsed.replace(day=1)
            return parsed.isoformat()
        except ValueError:
            continue
    return None


def normalise_date_key(value: str) -> str:
    return parse_loose_date(value) or normalise_key(value)


def extract_notice_date_from_url(url: str) -> Optional[str]:
    filename = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    stem = re.sub(r"\.pdf$", "", filename, flags=re.I)

    for pattern in [r"Week\s+Commencing\s+(.+)$", r"Week\s+Ending\s*(?:Fri\s*)?[- ]*(.+)$"]:
        match = re.search(pattern, stem, flags=re.I)
        if match:
            parsed = parse_loose_date(match.group(1))
            if parsed:
                return parsed

    candidates = []
    candidates.extend(re.findall(r"(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s*\d{4})", stem, flags=re.I))
    candidates.extend(re.findall(r"(\d{1,2}[-_/]\d{1,2}[-_/]\d{4})", stem))
    candidates.extend(re.findall(r"(\d{4}[-_/]\d{1,2}[-_/]\d{1,2})", stem))
    for candidate in reversed(candidates):
        parsed = parse_loose_date(candidate.replace("_", "-").replace("/", "-"))
        if parsed:
            return parsed
    return None


def safe_filename_from_url(url: str) -> str:
    raw = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "weekly_notice.pdf"
    raw = re.sub(r"[<>:\"/\\|?*]+", "_", raw)
    if not raw.lower().endswith(".pdf"):
        raw += ".pdf"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"{raw[:-4]}__{digest}.pdf"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_pta_url(url: str) -> bool:
    return urlparse(url).netloc.lower().endswith(PTA_HOST_SUFFIX)


def extract_dnn_page_config(html: str) -> dict[str, str]:
    config: dict[str, str] = {}
    for key, pattern in {"module_id": r'moduleId:\s*"(\d+)"', "root_folder_id": r'rootFolderId:\s*"(\d+)"'}.items():
        match = re.search(pattern, html, flags=re.I)
        if match:
            config[key] = match.group(1)

    token_match = re.search(r'name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)["\']', html, flags=re.I)
    if token_match:
        config["request_verification_token"] = token_match.group(1)
    return config


def create_dnn_session(seed_url: str, module_id: str, tab_id: str):
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": f"{APP_NAME}/2.3 (+local research script)",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": seed_url,
            "moduleid": str(module_id),
            "tabid": str(tab_id),
        }
    )

    logging.info("Priming DNN session from %s", seed_url)
    response = session.get(seed_url, timeout=45)
    response.raise_for_status()
    config = extract_dnn_page_config(response.text)

    session.headers.update({"moduleid": config.get("module_id", module_id), "tabid": config.get("tab_id", tab_id)})
    if config.get("request_verification_token"):
        session.headers.update({"requestverificationtoken": config["request_verification_token"]})

    logging.info(
        "DNN config: moduleId=%s tabId=%s rootFolderId=%s token=%s",
        session.headers.get("moduleid"),
        session.headers.get("tabid"),
        config.get("root_folder_id", "not-found"),
        "found" if config.get("request_verification_token") else "not-found",
    )
    return session, config


def save_dnn_response_sample(folder_id: int, data: object) -> None:
    sample_path = DIAGNOSTICS_DIR / f"dnn_folder_{folder_id}.json"
    try:
        sample_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logging.debug("Unable to save DNN sample for folder %s: %s", folder_id, exc)


def get_dnn_folder_content(session, folder_id: int) -> dict[str, object]:
    params = {"startIndex": 0, "numItems": 2147483647, "sort": "Name asc", "folderId": folder_id}
    logging.info("DNN folder content folderId=%s", folder_id)
    response = session.get(DNN_CONTENT_ENDPOINT, params=params, timeout=60)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise PipelineError(f"Unexpected DNN response for folder {folder_id}: {type(data).__name__}")
    save_dnn_response_sample(folder_id, data)
    return data


def dnn_item_to_pdf(item: dict[str, object]) -> Optional[DiscoveredPdf]:
    if bool(item.get("IsFolder")):
        return None
    extension = normalise_space(item.get("Extension")).lower().lstrip(".")
    name = normalise_space(item.get("Name"))
    item_url = normalise_space(item.get("Url"))
    if extension != "pdf" or not item_url or not re.search(r"weekly\s*notice", name, flags=re.I):
        return None
    absolute_url = urljoin(PTA_BASE_URL, item_url)
    return DiscoveredPdf(absolute_url, safe_filename_from_url(absolute_url), extract_notice_date_from_url(absolute_url))


def discover_dnn_pdf_links(seed_url: str, folder_ids: list[int], module_id: str, tab_id: str, max_folders: int = 10000) -> list[DiscoveredPdf]:
    session, config = create_dnn_session(seed_url, module_id, tab_id)
    start_folders = folder_ids or DEFAULT_DNN_FOLDER_IDS
    if not start_folders and config.get("root_folder_id"):
        start_folders = [int(config["root_folder_id"])]

    queue = list(dict.fromkeys(int(folder_id) for folder_id in start_folders))
    seen_folders: set[int] = set()
    found: dict[str, DiscoveredPdf] = {}

    while queue:
        folder_id = queue.pop(0)
        if folder_id in seen_folders:
            continue
        if len(seen_folders) >= max_folders:
            raise PipelineError(f"DNN traversal stopped after max_folders={max_folders}")
        seen_folders.add(folder_id)

        try:
            content = get_dnn_folder_content(session, folder_id)
        except Exception as exc:
            logging.warning("Could not read DNN folder %s: %s", folder_id, exc)
            continue

        items = content.get("Items", [])
        if not isinstance(items, list):
            logging.warning("DNN folder %s did not contain an Items list", folder_id)
            continue

        child_folders = 0
        pdfs_here = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            if bool(item.get("IsFolder")):
                child_id = item.get("ItemId")
                if child_id is None:
                    logging.debug("Skipping folder without ItemId: %r", item)
                    continue
                try:
                    queue.append(int(child_id))
                    child_folders += 1
                except (TypeError, ValueError):
                    logging.debug("Skipping folder with non-numeric ItemId: %r", item)
                continue

            pdf = dnn_item_to_pdf(item)
            if pdf:
                found[pdf.url] = pdf
                pdfs_here += 1

        logging.info(
            "DNN folder %s: child_folders=%s weekly_notice_pdfs=%s total_found=%s",
            folder_id,
            child_folders,
            pdfs_here,
            len(found),
        )
        time.sleep(0.15)

    return sorted(found.values(), key=lambda item: (item.notice_date or "9999-99-99", item.url))


def discover_html_pdf_links(seed_urls: list[str], max_depth: int = 2) -> list[DiscoveredPdf]:
    session = requests.Session()
    session.headers.update({"User-Agent": f"{APP_NAME}/2.3"})
    seen_pages: set[str] = set()
    queue: list[tuple[str, int]] = [(url, 0) for url in seed_urls]
    found: dict[str, DiscoveredPdf] = {}

    while queue:
        page_url, depth = queue.pop(0)
        if page_url in seen_pages or depth > max_depth or not is_pta_url(page_url):
            continue
        seen_pages.add(page_url)
        logging.info("Fallback HTML discovery depth=%s url=%s", depth, page_url)
        try:
            response = session.get(page_url, timeout=45)
            response.raise_for_status()
        except Exception as exc:
            logging.warning("Could not fetch fallback page %s: %s", page_url, exc)
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        for anchor in soup.find_all("a", href=True):
            absolute = urljoin(page_url, normalise_space(anchor.get("href"))).split("#", 1)[0]
            if not is_pta_url(absolute):
                continue
            if ".pdf" in urlparse(absolute).path.lower() and re.search(r"weekly\s*notice", unquote(absolute), re.I):
                found[absolute] = DiscoveredPdf(absolute, safe_filename_from_url(absolute), extract_notice_date_from_url(absolute))
            elif depth < max_depth:
                path = urlparse(absolute).path.lower()
                if any(token in path for token in ["safety", "weekly", "working-with-the-pta"]):
                    queue.append((absolute, depth + 1))
    return sorted(found.values(), key=lambda item: (item.notice_date or "9999-99-99", item.url))


def register_pdfs(conn: sqlite3.Connection, pdfs: Iterable[DiscoveredPdf]) -> int:
    count = 0
    for pdf in pdfs:
        conn.execute(
            """
            INSERT INTO source_pdf (url, filename, url_hash, notice_date, discovered_at, status)
            VALUES (?, ?, ?, ?, ?, 'discovered')
            ON CONFLICT(url) DO UPDATE SET
                filename=excluded.filename,
                notice_date=COALESCE(source_pdf.notice_date, excluded.notice_date)
            """,
            (pdf.url, pdf.filename, hashlib.sha256(pdf.url.encode("utf-8")).hexdigest(), pdf.notice_date, now_iso()),
        )
        count += 1
    conn.commit()
    return count


def build_download_candidates(url: str) -> list[str]:
    split = urlsplit(url)
    path = unquote(split.path)
    path_variants = [path]

    replacements = {
        "Febuary": "February",
        "Week Ending- ": "Week Ending - ",
        "Week Ending-": "Week Ending -",
    }
    for old, new in replacements.items():
        if old in path:
            path_variants.append(path.replace(old, new))

    candidates: list[str] = []
    for path_variant in path_variants:
        encoded_path = quote(path_variant, safe="/%")
        candidates.append(urlunsplit((split.scheme, split.netloc, encoded_path, split.query, split.fragment)))
        candidates.append(urlunsplit((split.scheme, split.netloc, encoded_path, "", split.fragment)))

    unique_candidates = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate not in seen:
            unique_candidates.append(candidate)
            seen.add(candidate)
    return unique_candidates


def download_source_pdf(conn: sqlite3.Connection, source: sqlite3.Row, force: bool = False) -> Path:
    local_path = PDF_DIR / source["filename"]
    if local_path.exists() and not force:
        return local_path

    candidates = build_download_candidates(str(source["url"]))
    last_error = ""
    for index, candidate_url in enumerate(candidates, start=1):
        logging.info("Downloading candidate %s/%s: %s", index, len(candidates), candidate_url)
        try:
            response = requests.get(candidate_url, timeout=120, allow_redirects=True)
        except Exception as exc:
            last_error = str(exc)
            logging.debug("Download candidate failed before response: %s", exc)
            continue

        if response.status_code == 404:
            last_error = f"404 Not Found: {candidate_url}"
            logging.debug("Download candidate returned 404: %s", candidate_url)
            continue

        try:
            response.raise_for_status()
        except Exception as exc:
            last_error = str(exc)
            continue

        if not response.content.startswith(b"%PDF"):
            last_error = f"Downloaded content is not a PDF from {candidate_url}"
            logging.debug(last_error)
            continue

        local_path.write_bytes(response.content)
        conn.execute(
            """
            UPDATE source_pdf
            SET local_path=?, file_sha256=?, downloaded_at=?, status='downloaded', last_error=NULL
            WHERE source_pdf_id=?
            """,
            (str(local_path), sha256_bytes(response.content), now_iso(), source["source_pdf_id"]),
        )
        conn.commit()
        return local_path

    raise MissingPdfError(f"Unable to download PDF after {len(candidates)} candidate URL(s). Last error: {last_error}")


def split_location_distance(raw_location: str, fallback_distance: str = "") -> tuple[str, str]:
    raw = normalise_space(raw_location)
    fallback_distance = normalise_space(fallback_distance)
    if fallback_distance:
        return raw, fallback_distance
    match = re.search(r"\d+(?:\.\d+)?\s*km", raw, flags=re.I)
    if match:
        location = raw[: match.start()].strip(" -")
        distance = raw[match.start():].strip()
        return location or raw, distance
    return raw, ""


def clean_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", normalise_space(value).lower()).strip()


def table_looks_like_tsr(table: Sequence[Sequence[Any]]) -> bool:
    flat = " ".join(clean_header(str(cell)) for row in table[:3] for cell in row if cell is not None)
    return "speed" in flat and ("restriction" in flat or "railway" in flat or "location" in flat)


def map_table_columns(header_row: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, header in enumerate(header_row):
        h = clean_header(header)
        if "location" in h or "to and from" in h:
            mapping["location_distance"] = index
        elif "section" in h and "railway" in h:
            mapping["line_section"] = index
        elif "distance" in h or "between" in h:
            mapping["distance"] = index
        elif "stn" in h:
            mapping["stn_no"] = index
        elif "maximum" in h and "speed" in h:
            mapping["max_speed"] = index
        elif "date" in h and "imposed" in h:
            mapping["date_imposed"] = index
        elif "reason" in h:
            mapping["reason"] = index
        elif "cancel" in h:
            mapping["date_cancelled"] = index
    return mapping


def get_cell(row: list[str], index: Optional[int]) -> str:
    if index is None or index >= len(row):
        return ""
    return normalise_space(row[index])


def extract_rows_from_pdf(pdf_path: Path, notice_date: Optional[str]) -> tuple[list[ExtractedTsrRow], int, int]:
    rows: list[ExtractedTsrRow] = []
    table_count = 0

    with pdfplumber.open(str(pdf_path)) as pdf:
        page_count = len(pdf.pages)
        for page_number, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if not re.search(r"Current\s+(?:Temporary\s+)?Speed\s+Restrictions|Date\s+to\s+be\s+Cancelled", text, re.I):
                continue

            tables = page.extract_tables(
                table_settings={
                    "vertical_strategy": "lines",
                    "horizontal_strategy": "lines",
                    "snap_tolerance": 3,
                    "join_tolerance": 3,
                    "edge_min_length": 3,
                    "intersection_tolerance": 5,
                }
            ) or []

            for table in tables:
                cleaned = [[normalise_space(cell) for cell in row] for row in table if row]
                cleaned = [row for row in cleaned if any(row)]
                if len(cleaned) < 2 or not table_looks_like_tsr(cleaned):
                    continue
                table_count += 1

                header_index = 0
                for i, candidate in enumerate(cleaned[:3]):
                    if table_looks_like_tsr([candidate]):
                        header_index = i
                        break

                mapping = map_table_columns(cleaned[header_index])
                for row_number, row in enumerate(cleaned[header_index + 1:], start=1):
                    if table_looks_like_tsr([row]):
                        continue

                    if "location_distance" in mapping:
                        location, distance = split_location_distance(get_cell(row, mapping.get("location_distance")))
                        line_section = location
                    else:
                        line_section = get_cell(row, mapping.get("line_section"))
                        location, distance = split_location_distance(line_section, get_cell(row, mapping.get("distance")))

                    stn_no = get_cell(row, mapping.get("stn_no"))
                    max_speed = get_cell(row, mapping.get("max_speed")) or (row[2] if len(row) >= 3 else "")
                    date_imposed = get_cell(row, mapping.get("date_imposed"))
                    reason = get_cell(row, mapping.get("reason"))
                    date_cancelled = get_cell(row, mapping.get("date_cancelled"))

                    if not date_imposed:
                        dates = [cell for cell in row if parse_loose_date(cell)]
                        date_imposed = dates[0] if dates else ""
                    if not reason:
                        candidates = [cell for cell in row if cell and "km/h" not in cell.lower() and not parse_loose_date(cell)]
                        reason = candidates[-1] if candidates else ""

                    if not (location or distance or max_speed or reason):
                        continue

                    rows.append(
                        ExtractedTsrRow(
                            notice_date=notice_date,
                            location=location,
                            line_section=line_section,
                            distance_km=distance,
                            stn_no=stn_no,
                            max_speed=max_speed,
                            date_imposed=date_imposed,
                            reason=reason,
                            date_cancelled=date_cancelled,
                            source_page=page_number,
                            source_row_number=row_number,
                            raw_row_json=json.dumps(row, ensure_ascii=False),
                        )
                    )
    return rows, page_count, table_count


def master_fingerprint(row: ExtractedTsrRow) -> str:
    material = "|".join(
        [normalise_key(row.location or row.line_section), normalise_key(row.distance_km), normalise_key(row.stn_no), normalise_date_key(row.date_imposed)]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def row_fingerprint(row: ExtractedTsrRow) -> str:
    material = "|".join(
        [
            normalise_key(row.location),
            normalise_key(row.line_section),
            normalise_key(row.distance_km),
            normalise_key(row.stn_no),
            normalise_key(row.max_speed),
            normalise_date_key(row.date_imposed),
            normalise_key(row.reason),
            normalise_key(row.date_cancelled),
            str(row.source_page),
            str(row.source_row_number),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def upsert_master(conn: sqlite3.Connection, row: ExtractedTsrRow) -> int:
    fp = master_fingerprint(row)
    stamp = now_iso()
    conn.execute(
        """
        INSERT INTO tsr_master (
            master_fingerprint, location_key, distance_key, stn_key, date_imposed_key,
            first_seen_notice_date, last_seen_notice_date, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(master_fingerprint) DO UPDATE SET
            first_seen_notice_date=MIN(COALESCE(tsr_master.first_seen_notice_date, excluded.first_seen_notice_date), COALESCE(excluded.first_seen_notice_date, tsr_master.first_seen_notice_date)),
            last_seen_notice_date=MAX(COALESCE(tsr_master.last_seen_notice_date, excluded.last_seen_notice_date), COALESCE(excluded.last_seen_notice_date, tsr_master.last_seen_notice_date)),
            updated_at=excluded.updated_at
        """,
        (
            fp,
            normalise_key(row.location or row.line_section),
            normalise_key(row.distance_km),
            normalise_key(row.stn_no),
            normalise_date_key(row.date_imposed),
            row.notice_date,
            row.notice_date,
            stamp,
            stamp,
        ),
    )
    result = conn.execute("SELECT tsr_master_id FROM tsr_master WHERE master_fingerprint=?", (fp,)).fetchone()
    return int(result["tsr_master_id"])


def process_source_pdf(conn: sqlite3.Connection, source: sqlite3.Row, force: bool = False) -> None:
    if source["status"] == "processed" and not force:
        logging.info("Skipping previously processed PDF: %s", source["filename"])
        return

    source_pdf_id = int(source["source_pdf_id"])
    try:
        path = download_source_pdf(conn, source, force=False)
        notice_date = source["notice_date"] or extract_notice_date_from_url(source["url"])
        extracted_rows, page_count, table_count = extract_rows_from_pdf(path, notice_date)

        if force:
            conn.execute("DELETE FROM tsr_occurrence WHERE source_pdf_id=?", (source_pdf_id,))

        for row in extracted_rows:
            master_id = upsert_master(conn, row)
            conn.execute(
                """
                INSERT OR IGNORE INTO tsr_occurrence (
                    tsr_master_id, source_pdf_id, notice_date, location, line_section, distance_km,
                    stn_no, max_speed, date_imposed, reason, date_cancelled, source_page,
                    source_row_number, row_fingerprint, raw_row_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    master_id,
                    source_pdf_id,
                    row.notice_date,
                    row.location,
                    row.line_section,
                    row.distance_km,
                    row.stn_no,
                    row.max_speed,
                    row.date_imposed,
                    row.reason,
                    row.date_cancelled,
                    row.source_page,
                    row.source_row_number,
                    row_fingerprint(row),
                    row.raw_row_json,
                    now_iso(),
                ),
            )

        conn.execute(
            """
            UPDATE source_pdf
            SET notice_date=COALESCE(?, notice_date), processed_at=?, status='processed', last_error=NULL,
                page_count=?, tsr_table_count=?, tsr_row_count=?
            WHERE source_pdf_id=?
            """,
            (notice_date, now_iso(), page_count, table_count, len(extracted_rows), source_pdf_id),
        )
        conn.commit()
        logging.info("Processed %s: pages=%s tables=%s rows=%s", source["filename"], page_count, table_count, len(extracted_rows))
    except MissingPdfError as exc:
        conn.rollback()
        logging.warning("Source PDF unavailable: %s", source["filename"])
        conn.execute("UPDATE source_pdf SET status='missing', last_error=? WHERE source_pdf_id=?", (str(exc), source_pdf_id))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logging.exception("Failed processing %s", source["filename"])
        conn.execute("UPDATE source_pdf SET status='failed', last_error=? WHERE source_pdf_id=?", (str(exc), source_pdf_id))
        conn.commit()


def export_csvs(conn: sqlite3.Connection) -> None:
    queries = {
        "pta_tsr_occurrences.csv": """
            SELECT
                o.tsr_record_id, o.tsr_master_id, o.notice_date, o.location, o.line_section,
                o.distance_km, o.stn_no, o.max_speed, o.date_imposed, o.reason,
                o.date_cancelled, s.filename AS source_pdf, s.url AS source_url,
                o.source_page, o.source_row_number
            FROM tsr_occurrence o
            JOIN source_pdf s ON s.source_pdf_id = o.source_pdf_id
            ORDER BY o.notice_date, o.tsr_record_id
        """,
        "pta_tsr_masters.csv": """
            SELECT
                m.tsr_master_id,
                MIN(o.notice_date) AS first_seen_notice_date,
                MAX(o.notice_date) AS last_seen_notice_date,
                COUNT(*) AS weeks_seen,
                (SELECT location FROM tsr_occurrence WHERE tsr_master_id=m.tsr_master_id ORDER BY notice_date DESC, tsr_record_id DESC LIMIT 1) AS latest_location,
                (SELECT line_section FROM tsr_occurrence WHERE tsr_master_id=m.tsr_master_id ORDER BY notice_date DESC, tsr_record_id DESC LIMIT 1) AS latest_line_section,
                (SELECT distance_km FROM tsr_occurrence WHERE tsr_master_id=m.tsr_master_id ORDER BY notice_date DESC, tsr_record_id DESC LIMIT 1) AS latest_distance_km,
                (SELECT stn_no FROM tsr_occurrence WHERE tsr_master_id=m.tsr_master_id ORDER BY notice_date DESC, tsr_record_id DESC LIMIT 1) AS latest_stn_no,
                (SELECT max_speed FROM tsr_occurrence WHERE tsr_master_id=m.tsr_master_id ORDER BY notice_date DESC, tsr_record_id DESC LIMIT 1) AS latest_max_speed,
                (SELECT date_imposed FROM tsr_occurrence WHERE tsr_master_id=m.tsr_master_id ORDER BY notice_date DESC, tsr_record_id DESC LIMIT 1) AS date_imposed,
                (SELECT reason FROM tsr_occurrence WHERE tsr_master_id=m.tsr_master_id ORDER BY notice_date DESC, tsr_record_id DESC LIMIT 1) AS latest_reason,
                (SELECT date_cancelled FROM tsr_occurrence WHERE tsr_master_id=m.tsr_master_id ORDER BY notice_date DESC, tsr_record_id DESC LIMIT 1) AS latest_date_cancelled,
                m.master_fingerprint
            FROM tsr_master m
            JOIN tsr_occurrence o ON o.tsr_master_id = m.tsr_master_id
            GROUP BY m.tsr_master_id
            ORDER BY first_seen_notice_date, m.tsr_master_id
        """,
        "pta_tsr_source_pdfs.csv": """
            SELECT source_pdf_id, notice_date, filename, url, status, tsr_table_count, tsr_row_count, last_error
            FROM source_pdf
            ORDER BY notice_date, filename
        """,
    }

    for filename, sql in queries.items():
        rows = conn.execute(sql).fetchall()
        path = EXPORT_DIR / filename
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            if rows:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows([dict(row) for row in rows])
            else:
                handle.write("")
        logging.info("Exported %s", path)


def print_status(conn: sqlite3.Connection) -> None:
    print("\nStatus summary")
    print("==============")
    for row in conn.execute("SELECT status, COUNT(*) AS count FROM source_pdf GROUP BY status ORDER BY status"):
        print(f"{row['status']}: {row['count']}")
    totals = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM source_pdf) AS pdfs,
            (SELECT COUNT(*) FROM tsr_master) AS masters,
            (SELECT COUNT(*) FROM tsr_occurrence) AS occurrences
        """
    ).fetchone()
    print(f"PDFs: {totals['pdfs']}")
    print(f"TSR masters: {totals['masters']}")
    print(f"TSR occurrences: {totals['occurrences']}")

    failed = conn.execute(
        "SELECT filename, last_error FROM source_pdf WHERE status IN ('failed', 'missing') ORDER BY status, filename LIMIT 20"
    ).fetchall()
    if failed:
        print("\nFailed or missing PDFs")
        print("----------------------")
        for row in failed:
            print(f"- {row['filename']}: {row['last_error']}")


def add_manual_pdf_urls(conn: sqlite3.Connection, urls: list[str]) -> None:
    pdfs = [DiscoveredPdf(url, safe_filename_from_url(url), extract_notice_date_from_url(url)) for url in urls]
    logging.info("Registered %s manually supplied PDF URL(s).", register_pdfs(conn, pdfs))


def add_pdf_list_file(conn: sqlite3.Connection, path: Path) -> None:
    if not path.exists():
        raise PipelineError(f"PDF list file does not exist: {path}")
    urls = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    add_manual_pdf_urls(conn, urls)


def create_diagnostics_zip() -> Path:
    """Create a flat diagnostics upload folder instead of a ZIP archive.

    The function name is retained so the diagnostics command remains compatible
    with earlier versions. The output is a single timestamped folder containing
    files that can be uploaded individually when ZIP upload is not supported.
    """
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = DATA_DIR / f"diagnostics_upload_{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest_lines = [
        "PTA TSR Collector diagnostics upload folder",
        f"Created: {dt.datetime.now().isoformat(timespec='seconds')}",
        f"Working directory: {ROOT}",
        f"Database path: {DB_PATH}",
        "",
        "Included files:",
    ]

    def copy_flat(source: Path, label: str = "") -> None:
        if not source.exists() or not source.is_file():
            return
        prefix = f"{label}__" if label else ""
        destination = output_dir / f"{prefix}{source.name}"
        counter = 1
        while destination.exists():
            destination = output_dir / f"{prefix}{source.stem}_{counter}{source.suffix}"
            counter += 1
        shutil.copy2(source, destination)
        manifest_lines.append(f"- {destination.name} <= {source}")

    def write_query_csv(filename: str, sql: str) -> None:
        if not DB_PATH.exists():
            return
        try:
            with sqlite3.connect(DB_PATH) as diag_conn:
                diag_conn.row_factory = sqlite3.Row
                rows = diag_conn.execute(sql).fetchall()
        except Exception as exc:
            error_path = output_dir / f"{filename}.error.txt"
            error_path.write_text(str(exc), encoding="utf-8")
            manifest_lines.append(f"- {error_path.name} <= generated query error")
            return

        destination = output_dir / filename
        with destination.open("w", newline="", encoding="utf-8-sig") as handle:
            if rows:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows([dict(row) for row in rows])
            else:
                handle.write("")
        manifest_lines.append(f"- {destination.name} <= generated from SQLite")

    copy_flat(DB_PATH, "database")
    copy_flat(LOG_PATH, "log")

    if EXPORT_DIR.exists():
        for file in sorted(EXPORT_DIR.glob("*.csv")):
            copy_flat(file, "export")

    if DIAGNOSTICS_DIR.exists():
        for file in sorted(DIAGNOSTICS_DIR.glob("*.json")):
            copy_flat(file, "dnn")
        for file in sorted(DIAGNOSTICS_DIR.glob("*.txt")):
            copy_flat(file, "diagnostics")

    write_query_csv(
        "generated__source_pdf_status_counts.csv",
        """
        SELECT status, COUNT(*) AS count
        FROM source_pdf
        GROUP BY status
        ORDER BY status
        """,
    )
    write_query_csv(
        "generated__missing_or_failed_pdfs.csv",
        """
        SELECT
            source_pdf_id,
            notice_date,
            filename,
            url,
            status,
            tsr_table_count,
            tsr_row_count,
            last_error
        FROM source_pdf
        WHERE status IN ('failed', 'missing')
        ORDER BY status, notice_date, filename
        """,
    )
    write_query_csv(
        "generated__recent_source_pdfs.csv",
        """
        SELECT
            source_pdf_id,
            notice_date,
            filename,
            url,
            status,
            tsr_table_count,
            tsr_row_count,
            last_error
        FROM source_pdf
        ORDER BY source_pdf_id DESC
        LIMIT 100
        """,
    )

    manifest_path = output_dir / "MANIFEST.txt"
    manifest_path.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
    return output_dir


def run_discovery(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    seeds = args.seed_url or [DEFAULT_SEED_URL]
    discovered: dict[str, DiscoveredPdf] = {}

    if not args.no_dnn:
        for seed in seeds:
            dnn_pdfs = discover_dnn_pdf_links(
                seed_url=seed,
                folder_ids=args.folder_id or DEFAULT_DNN_FOLDER_IDS,
                module_id=args.module_id,
                tab_id=args.tab_id,
            )
            discovered.update({pdf.url: pdf for pdf in dnn_pdfs})
        logging.info("DNN discovery found %s unique Weekly Notice PDF link(s).", len(discovered))

    if args.html_fallback:
        fallback_pdfs = discover_html_pdf_links(seeds, max_depth=args.max_depth)
        discovered.update({pdf.url: pdf for pdf in fallback_pdfs})
        logging.info("Fallback HTML discovery added %s PDF link(s).", len(fallback_pdfs))

    logging.info("Discovery complete. Found/registered %s PDF link(s).", register_pdfs(conn, discovered.values()))


def run_processing(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    statuses = ["discovered", "downloaded"]
    if args.retry_failed:
        statuses.append("failed")
    if args.retry_missing:
        statuses.append("missing")

    if args.force:
        query = "SELECT * FROM source_pdf ORDER BY notice_date, filename"
        params: tuple[object, ...] = ()
    else:
        placeholders = ",".join("?" for _ in statuses)
        query = f"SELECT * FROM source_pdf WHERE status IN ({placeholders}) ORDER BY notice_date, filename"
        params = tuple(statuses)

    sources = conn.execute(query, params).fetchall()
    if args.limit:
        sources = sources[: args.limit]
    logging.info("Processing %s PDF(s).", len(sources))
    for source in sources:
        process_source_pdf(conn, source, force=args.force)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="PTA Weekly Notices TSR extraction pipeline v2.3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            r"""
            Common commands:
              py .\pta_tsr_collector.py run --install-deps
              py .\pta_tsr_collector.py discover --folder-id 5160
              py .\pta_tsr_collector.py process --retry-failed
              py .\pta_tsr_collector.py process --retry-missing
              py .\pta_tsr_collector.py status
              py .\pta_tsr_collector.py diagnostics
            """
        ),
    )
    parser.add_argument("command", choices=["init", "discover", "process", "run", "export", "status", "diagnostics"])
    parser.add_argument("--seed-url", action="append", default=[], help="Page used to prime the DNN session. Can be repeated.")
    parser.add_argument("--folder-id", action="append", type=int, default=[], help="DNN folder ID to traverse. Can be repeated. Default: 5160.")
    parser.add_argument("--module-id", default=DEFAULT_DNN_MODULE_ID, help="DNN module ID. Default: 3195.")
    parser.add_argument("--tab-id", default=DEFAULT_DNN_TAB_ID, help="DNN tab ID. Default: 1198.")
    parser.add_argument("--no-dnn", action="store_true", help="Disable DNN API discovery.")
    parser.add_argument("--html-fallback", action="store_true", help="Also run generic HTML PDF discovery after DNN discovery.")
    parser.add_argument("--pdf-url", action="append", default=[], help="Direct Weekly Notice PDF URL. Can be repeated.")
    parser.add_argument("--pdf-list", type=Path, help="Text file containing one PDF URL per line.")
    parser.add_argument("--max-depth", type=int, default=2, help="Fallback HTML crawl depth. Default: 2.")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of PDFs to process this run. 0 means no limit.")
    parser.add_argument("--retry-failed", action="store_true", help="Retry PDFs currently marked failed.")
    parser.add_argument("--retry-missing", action="store_true", help="Retry PDFs currently marked missing.")
    parser.add_argument("--force", action="store_true", help="Force reprocessing of already processed PDFs.")
    parser.add_argument("--install-deps", action="store_true", help="Install missing Python packages without prompting.")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging.")
    args = parser.parse_args(argv)

    try:
        ensure_dependencies(auto_install=args.install_deps)
        import_runtime_dependencies()
        setup_dirs_and_logging(verbose=args.verbose)

        conn = connect_db()
        init_db(conn)

        if args.pdf_url:
            add_manual_pdf_urls(conn, args.pdf_url)
        if args.pdf_list:
            add_pdf_list_file(conn, args.pdf_list)

        if args.command == "init":
            logging.info("Initialised database at %s", DB_PATH)
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
            diagnostics_folder = create_diagnostics_zip()
            logging.info("Created diagnostics folder: %s", diagnostics_folder)
            print(diagnostics_folder)
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
