#!/usr/bin/env python3
"""Export PTA TSR rejection diagnostics from a live/partial collector SQLite DB.

Usage from the repository root:
    py .\export_rejection_diagnostics.py

Optional:
    py .\export_rejection_diagnostics.py --db .\pta_tsr_data\pta_tsr.sqlite3 --out .\pta_tsr_data\diagnostics
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sqlite3
from pathlib import Path


def write_csv(path: Path, rows: list[sqlite3.Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        if not rows:
            handle.write("")
            return
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows([dict(row) for row in rows])


def main() -> int:
    parser = argparse.ArgumentParser(description="Export PTA TSR rejection diagnostics from SQLite")
    parser.add_argument("--db", type=Path, default=Path("pta_tsr_data") / "pta_tsr.sqlite3")
    parser.add_argument("--out", type=Path, default=Path("pta_tsr_data") / "diagnostics")
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"Database not found: {args.db}")

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = args.out / f"rejection_review_{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    queries = {
        "00_reject_reason_summary.csv": """
            SELECT
                reject_reason,
                COUNT(*) AS rejected_rows,
                MIN(notice_date) AS first_notice_date,
                MAX(notice_date) AS last_notice_date
            FROM tsr_rejected_row
            GROUP BY reject_reason
            ORDER BY rejected_rows DESC, reject_reason
        """,
        "01_rejected_rows_recent.csv": """
            SELECT
                r.rejected_row_id,
                r.notice_date,
                s.filename AS source_pdf,
                r.source_page,
                r.source_row_number,
                r.reject_reason,
                r.raw_row_json
            FROM tsr_rejected_row r
            JOIN source_pdf s ON s.source_pdf_id = r.source_pdf_id
            ORDER BY r.rejected_row_id DESC
            LIMIT 1000
        """,
        "02_rejected_rows_sample_by_reason.csv": """
            WITH ranked AS (
                SELECT
                    r.rejected_row_id,
                    r.notice_date,
                    s.filename AS source_pdf,
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
            SELECT *
            FROM ranked
            WHERE rn <= 100
            ORDER BY reject_reason, notice_date, source_pdf, source_page, source_row_number
        """,
        "03_pdf_processing_counts.csv": """
            SELECT
                source_pdf_id,
                notice_date,
                filename,
                status,
                tsr_table_count,
                tsr_row_count,
                rejected_row_count,
                last_error
            FROM source_pdf
            ORDER BY notice_date, filename
        """,
        "04_low_acceptance_pdfs.csv": """
            SELECT
                source_pdf_id,
                notice_date,
                filename,
                status,
                tsr_row_count AS accepted_rows,
                rejected_row_count AS rejected_rows,
                CASE
                    WHEN COALESCE(tsr_row_count, 0) + COALESCE(rejected_row_count, 0) = 0 THEN NULL
                    ELSE ROUND(
                        CAST(rejected_row_count AS REAL) /
                        (CAST(tsr_row_count AS REAL) + CAST(rejected_row_count AS REAL)),
                        3
                    )
                END AS rejected_ratio,
                last_error
            FROM source_pdf
            WHERE COALESCE(rejected_row_count, 0) > 0
            ORDER BY rejected_ratio DESC, rejected_rows DESC, notice_date
        """,
        "05_accepted_rows_recent.csv": """
            SELECT
                o.tsr_record_id,
                o.tsr_master_id,
                o.notice_date,
                s.filename AS source_pdf,
                o.source_page,
                o.source_row_number,
                o.line_key,
                o.line_name,
                o.location_direction,
                o.affected_area,
                o.location,
                o.distance_km,
                o.stn_no,
                o.max_speed,
                o.date_imposed AS date_imposed_raw,
                o.date_imposed_normalised,
                o.reason AS reason_raw,
                o.reason_group,
                o.date_cancelled AS date_cancelled_raw,
                o.date_cancelled_normalised,
                o.row_quality,
                o.normalisation_notes,
                o.raw_row_json
            FROM tsr_occurrence o
            JOIN source_pdf s ON s.source_pdf_id = o.source_pdf_id
            ORDER BY o.tsr_record_id DESC
            LIMIT 1000
        """,
    }

    for filename, sql in queries.items():
        rows = conn.execute(sql).fetchall()
        write_csv(output_dir / filename, rows)

    manifest = output_dir / "README.txt"
    manifest.write_text(
        "PTA TSR rejection diagnostics\n"
        f"Created: {dt.datetime.now().isoformat(timespec='seconds')}\n"
        f"Database: {args.db}\n\n"
        "Send this entire folder for review if rejection rates look wrong.\n",
        encoding="utf-8",
    )

    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
