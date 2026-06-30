#!/usr/bin/env python3
"""PTA TSR rejection diagnostics compact exporter v2
====================================================

Creates exactly three upload-friendly CSV files for Copilot review.

Why this exists:
- Copilot upload limit: three files per message.
- ZIP uploads are not accepted.
- The collector can be cancelled mid-run, so diagnostics must be readable direct from the live SQLite database before final export files exist.

Usage from repository root:
- py .\export_rejection_diagnostics_compact.py

Optional:
- py .\export_rejection_diagnostics_compact.py --db .\pta_tsr_data\pta_tsr.sqlite3 --out .\pta_tsr_data\diagnostics

Outputs exactly three CSV files:
- 01_rejection_summary.csv
- 02_rejection_samples.csv
- 03_manual_review_template.csv
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_DB = Path("pta_tsr_data") / "pta_tsr.sqlite3"
DEFAULT_OUT = Path("pta_tsr_data") / "diagnostics"

SPEED_RE = re.compile(r"\b\d{1,3}\s*(?:km\s*/?\s*h|kmh|kph)\b", re.IGNORECASE)
KM_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,4})?\s*km\b", re.IGNORECASE)
DATE_RE = re.compile(
    r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}|[A-Za-z]{3,9}\s+\d{4})\b",
    re.IGNORECASE,
)
STN_RE = re.compile(r"\b\d{2}-\d{2}-\d{3}\b")
BAD_REASON_RE = re.compile(
    r"^(?:TBA|TBC|TBD|N/A|INDEFINITE|PERMANENT TSR|STN NO\.?|UP MAIN|DOWN MAIN|DIRECTION|UP DIRECTION|DOWN DIRECTION|\d{2}-\d{2}-\d{3}|\d{1,2}/\d{1,2}/\d{2,4}|\d+(?:\.\d+)?\s*KM.*)$",
    re.IGNORECASE,
)


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        if not fieldnames:
            fieldnames = list(rows[0].keys()) if rows else ["message"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        if rows:
            writer.writerows(rows)


def scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def safe_json_list(raw_json: str) -> list[str]:
    try:
        parsed = json.loads(raw_json or "[]")
        if isinstance(parsed, list):
            return ["" if item is None else str(item) for item in parsed]
    except Exception:
        pass
    return []


def cell_preview(cells: list[str], limit: int = 12) -> str:
    return " | ".join(cells[:limit])


def detect_features(cells: list[str]) -> dict[str, Any]:
    joined = " | ".join(cells)
    speed_cells = [cell for cell in cells if SPEED_RE.search(cell)]
    km_cells = [cell for cell in cells if KM_RE.search(cell)]
    date_cells = [cell for cell in cells if DATE_RE.search(cell)]
    stn_cells = [cell for cell in cells if STN_RE.search(cell)]
    bad_reason_like_cells = [cell for cell in cells if BAD_REASON_RE.match(cell.strip())]
    nonblank = [cell for cell in cells if cell.strip()]

    return {
        "raw_cell_count": len(cells),
        "nonblank_cell_count": len(nonblank),
        "has_speed_anywhere": 1 if speed_cells else 0,
        "speed_candidates": "; ".join(speed_cells[:5]),
        "has_km_anywhere": 1 if km_cells else 0,
        "km_candidates": "; ".join(km_cells[:5]),
        "has_date_anywhere": 1 if date_cells else 0,
        "date_candidates": "; ".join(date_cells[:5]),
        "has_stn_anywhere": 1 if stn_cells else 0,
        "stn_candidates": "; ".join(stn_cells[:5]),
        "bad_reason_like_cells": "; ".join(bad_reason_like_cells[:5]),
        "row_text": joined,
        "cell_preview": cell_preview(cells),
    }


def suggested_action(reject_reason: str, features: dict[str, Any]) -> str:
    if reject_reason == "missing_or_invalid_speed" and features["has_speed_anywhere"]:
        return "auto_repair_candidate_speed_in_other_cell"
    if reject_reason == "missing_or_invalid_speed" and not features["has_speed_anywhere"]:
        return "manual_review_no_speed_detected"
    if reject_reason == "missing_km_location" and features["has_speed_anywhere"] and features["nonblank_cell_count"] >= 4:
        return "auto_accept_candidate_named_location_no_km"
    if reject_reason == "invalid_reason_or_shifted_columns" and features["has_speed_anywhere"]:
        return "manual_review_probable_shifted_columns"
    return "manual_review"


def build_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    summary_rows = []
    totals = {
        "source_pdfs_total": scalar(conn, "SELECT COUNT(*) FROM source_pdf"),
        "source_pdfs_processed": scalar(conn, "SELECT COUNT(*) FROM source_pdf WHERE status='processed'"),
        "accepted_rows_total": scalar(conn, "SELECT COUNT(*) FROM tsr_occurrence"),
        "rejected_rows_total": scalar(conn, "SELECT COUNT(*) FROM tsr_rejected_row"),
        "latest_processed_notice_date": scalar(conn, "SELECT MAX(notice_date) FROM source_pdf WHERE status='processed'"),
    }

    for metric, value in totals.items():
        summary_rows.append({
            "section": "overall",
            "metric": metric,
            "value": value,
            "reject_reason": "",
            "first_notice_date": "",
            "last_notice_date": "",
            "accepted_rows": "",
            "rejected_rows": "",
            "rejected_ratio": "",
            "notes": "",
        })

    # Reject reason summary.
    for row in conn.execute(
        """
        SELECT reject_reason, COUNT(*) AS rejected_rows, MIN(notice_date) AS first_notice_date, MAX(notice_date) AS last_notice_date
        FROM tsr_rejected_row
        GROUP BY reject_reason
        ORDER BY rejected_rows DESC, reject_reason
        """
    ):
        summary_rows.append({
            "section": "reject_reason",
            "metric": "rejected_rows_by_reason",
            "value": row["rejected_rows"],
            "reject_reason": row["reject_reason"],
            "first_notice_date": row["first_notice_date"],
            "last_notice_date": row["last_notice_date"],
            "accepted_rows": "",
            "rejected_rows": row["rejected_rows"],
            "rejected_ratio": "",
            "notes": "",
        })

    # PDF acceptance/rejection roll-up, worst first.
    for row in conn.execute(
        """
        SELECT
            source_pdf_id,
            notice_date,
            filename,
            status,
            COALESCE(tsr_row_count, 0) AS accepted_rows,
            COALESCE(rejected_row_count, 0) AS rejected_rows,
            CASE
                WHEN COALESCE(tsr_row_count, 0) + COALESCE(rejected_row_count, 0) = 0 THEN NULL
                ELSE ROUND(CAST(rejected_row_count AS REAL) / (CAST(tsr_row_count AS REAL) + CAST(rejected_row_count AS REAL)), 3)
            END AS rejected_ratio
        FROM source_pdf
        WHERE COALESCE(rejected_row_count, 0) > 0 OR COALESCE(tsr_row_count, 0) = 0
        ORDER BY rejected_ratio DESC, rejected_rows DESC, notice_date
        LIMIT 300
        """
    ):
        summary_rows.append({
            "section": "pdf_quality_top_300",
            "metric": row["filename"],
            "value": row["rejected_ratio"],
            "reject_reason": "",
            "first_notice_date": row["notice_date"],
            "last_notice_date": row["notice_date"],
            "accepted_rows": row["accepted_rows"],
            "rejected_rows": row["rejected_rows"],
            "rejected_ratio": row["rejected_ratio"],
            "notes": f"source_pdf_id={row['source_pdf_id']}; status={row['status']}",
        })

    return summary_rows


def build_samples(conn: sqlite3.Connection, per_reason: int, recent_limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    # Balanced samples per reason, earliest first so layout-era issues are visible.
    query = f"""
        WITH ranked AS (
            SELECT
                r.rejected_row_id,
                r.notice_date,
                s.filename AS source_pdf,
                s.source_pdf_id,
                r.source_page,
                r.source_row_number,
                r.reject_reason,
                r.raw_row_json,
                ROW_NUMBER() OVER (
                    PARTITION BY r.reject_reason
                    ORDER BY r.notice_date, s.filename, r.source_page, r.source_row_number
                ) AS rn
            FROM tsr_rejected_row r
            JOIN source_pdf s ON s.source_pdf_id = r.source_pdf_id
        )
        SELECT * FROM ranked
        WHERE rn <= ?
        ORDER BY reject_reason, notice_date, source_pdf, source_page, source_row_number
    """
    for row in conn.execute(query, (per_reason,)):
        cells = safe_json_list(row["raw_row_json"])
        features = detect_features(cells)
        rows.append({
            "sample_type": "balanced_by_reason",
            "rejected_row_id": row["rejected_row_id"],
            "notice_date": row["notice_date"],
            "source_pdf_id": row["source_pdf_id"],
            "source_pdf": row["source_pdf"],
            "source_page": row["source_page"],
            "source_row_number": row["source_row_number"],
            "reject_reason": row["reject_reason"],
            **features,
            "suggested_action": suggested_action(row["reject_reason"], features),
            "raw_row_json": row["raw_row_json"],
        })

    # Recent samples, because later layout failures may differ.
    for row in conn.execute(
        """
        SELECT
            r.rejected_row_id,
            r.notice_date,
            s.filename AS source_pdf,
            s.source_pdf_id,
            r.source_page,
            r.source_row_number,
            r.reject_reason,
            r.raw_row_json
        FROM tsr_rejected_row r
        JOIN source_pdf s ON s.source_pdf_id = r.source_pdf_id
        ORDER BY r.rejected_row_id DESC
        LIMIT ?
        """,
        (recent_limit,),
    ):
        cells = safe_json_list(row["raw_row_json"])
        features = detect_features(cells)
        rows.append({
            "sample_type": "recent",
            "rejected_row_id": row["rejected_row_id"],
            "notice_date": row["notice_date"],
            "source_pdf_id": row["source_pdf_id"],
            "source_pdf": row["source_pdf"],
            "source_page": row["source_page"],
            "source_row_number": row["source_row_number"],
            "reject_reason": row["reject_reason"],
            **features,
            "suggested_action": suggested_action(row["reject_reason"], features),
            "raw_row_json": row["raw_row_json"],
        })

    return rows


def build_manual_review_template(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in conn.execute(
        """
        SELECT
            r.rejected_row_id,
            r.notice_date,
            s.filename AS source_pdf,
            s.source_pdf_id,
            r.source_page,
            r.source_row_number,
            r.reject_reason,
            r.raw_row_json
        FROM tsr_rejected_row r
        JOIN source_pdf s ON s.source_pdf_id = r.source_pdf_id
        ORDER BY
            CASE r.reject_reason
                WHEN 'missing_or_invalid_speed' THEN 1
                WHEN 'missing_km_location' THEN 2
                WHEN 'invalid_reason_or_shifted_columns' THEN 3
                ELSE 9
            END,
            r.notice_date,
            s.filename,
            r.source_page,
            r.source_row_number
        LIMIT ?
        """,
        (limit,),
    ):
        cells = safe_json_list(row["raw_row_json"])
        features = detect_features(cells)
        rows.append({
            "rejected_row_id": row["rejected_row_id"],
            "notice_date": row["notice_date"],
            "source_pdf_id": row["source_pdf_id"],
            "source_pdf": row["source_pdf"],
            "source_page": row["source_page"],
            "source_row_number": row["source_row_number"],
            "reject_reason": row["reject_reason"],
            "suggested_action": suggested_action(row["reject_reason"], features),
            "cell_preview": features["cell_preview"],
            "speed_candidates": features["speed_candidates"],
            "km_candidates": features["km_candidates"],
            "date_candidates": features["date_candidates"],
            "stn_candidates": features["stn_candidates"],
            "accept_row": "",
            "corrected_location": "",
            "corrected_distance_km": "",
            "corrected_stn_no": "",
            "corrected_max_speed": "",
            "corrected_date_imposed": "",
            "corrected_reason": "",
            "corrected_date_cancelled": "",
            "corrected_line_key": "",
            "corrected_line_name": "",
            "corrected_location_direction": "",
            "review_notes": "",
            "raw_row_json": row["raw_row_json"],
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Export compact PTA TSR rejection diagnostics within Copilot upload limits")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--per-reason", type=int, default=80, help="Balanced rejected-row samples per reject reason")
    parser.add_argument("--recent", type=int, default=200, help="Recent rejected-row samples")
    parser.add_argument("--manual-limit", type=int, default=500, help="Rows in manual review template")
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"Database not found: {args.db}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = args.out / f"rejection_review_compact_{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = build_summary(conn)
    samples = build_samples(conn, args.per_reason, args.recent)
    manual = build_manual_review_template(conn, args.manual_limit)

    write_csv(output_dir / "01_rejection_summary.csv", summary)
    write_csv(output_dir / "02_rejection_samples.csv", samples)
    write_csv(output_dir / "03_manual_review_template.csv", manual)

    print(output_dir)
    print("Created exactly three CSV files for upload:")
    print(output_dir / "01_rejection_summary.csv")
    print(output_dir / "02_rejection_samples.csv")
    print(output_dir / "03_manual_review_template.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
