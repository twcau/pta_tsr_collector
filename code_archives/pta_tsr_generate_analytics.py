#!/usr/bin/env python3
r"""
PTA TSR Analytics Export Generator
==================================

Reads the collector CSV exports and creates dashboard-ready analytics CSVs.

Default input:
    pta_tsr_data\exports

Default output:
    pta_tsr_data\analytics

Usage:
    py .\pta_tsr_generate_analytics.py

Optional:
    py .\pta_tsr_generate_analytics.py --exports-dir .\pta_tsr_data\exports --analytics-dir .\pta_tsr_data\analytics
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Optional

import pandas as pd

DEFAULT_EXPORTS_DIR = Path("pta_tsr_data") / "exports"
DEFAULT_ANALYTICS_DIR = Path("pta_tsr_data") / "analytics"


def clean_text(value: object) -> str:
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def parse_date_series(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce", format="mixed")


def extract_line_key(row: pd.Series) -> str:
    candidates = [
        clean_text(row.get("line_section", "")),
        clean_text(row.get("location", "")),
        clean_text(row.get("latest_line_section", "")),
        clean_text(row.get("latest_location", "")),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        # Prefer short line/direction codes such as MTMDN, NSR, FRE, etc.
        token_match = re.match(r"^([A-Za-z0-9]{2,12})\b", candidate)
        if token_match:
            return token_match.group(1).upper()
        return candidate.upper()
    return "UNKNOWN"


def normalise_reason(reason: object) -> str:
    text = clean_text(reason).lower()
    if not text:
        return "Unknown / not stated"

    patterns = [
        ("Signals / train control", ["signal", "train control", "points", "point machine", "interlocking"]),
        ("Track condition", ["track", "rail", "sleeper", "ballast", "formation", "geometry", "alignment", "gauge", "dip", "top"]),
        ("Level crossing / pedestrian crossing", ["crossing", "pedestrian"]),
        ("Structures / bridge", ["bridge", "structure", "culvert", "underpass", "overpass", "tunnel"]),
        ("Turnout / points", ["turnout", "points", "switch"]),
        ("Works / project", ["works", "construction", "project", "commissioning", "possession", "temporary works"]),
        ("Overhead / electrical", ["overhead", "traction", "electrical", "power", "ocls", "ole"]),
        ("Platform / station", ["platform", "station"]),
    ]
    for label, needles in patterns:
        if any(needle in text for needle in needles):
            return label
    return "Other / review required"


def duration_bucket(weeks: object) -> str:
    try:
        value = float(weeks)
    except (TypeError, ValueError):
        return "Unknown"
    if value <= 1:
        return "1 week"
    if value <= 4:
        return "2-4 weeks"
    if value <= 13:
        return "1-3 months"
    if value <= 26:
        return "3-6 months"
    if value <= 52:
        return "6-12 months"
    if value <= 104:
        return "1-2 years"
    if value <= 260:
        return "2-5 years"
    return "5+ years"


def add_date_parts(df: pd.DataFrame, date_col: str = "notice_date") -> pd.DataFrame:
    df = df.copy()
    df[date_col] = parse_date_series(df[date_col])
    df["year"] = df[date_col].dt.year.astype("Int64")
    df["month"] = df[date_col].dt.month.astype("Int64")
    df["year_month"] = df[date_col].dt.to_period("M").astype(str).replace("NaT", "")
    return df


def median_or_blank(series: pd.Series) -> Optional[float]:
    value = pd.to_numeric(series, errors="coerce").median()
    if pd.isna(value):
        return None
    return float(value)


def create_analytics(exports_dir: Path, analytics_dir: Path) -> None:
    analytics_dir.mkdir(parents=True, exist_ok=True)

    occurrences_path = exports_dir / "pta_tsr_occurrences.csv"
    masters_path = exports_dir / "pta_tsr_masters.csv"
    sources_path = exports_dir / "pta_tsr_source_pdfs.csv"

    missing = [str(path) for path in [occurrences_path, masters_path, sources_path] if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing required export CSV(s): " + ", ".join(missing))

    occurrences = pd.read_csv(occurrences_path, dtype=str, keep_default_na=False)
    masters = pd.read_csv(masters_path, dtype=str, keep_default_na=False)
    sources = pd.read_csv(sources_path, dtype=str, keep_default_na=False)

    occurrences = add_date_parts(occurrences, "notice_date")
    masters["first_seen_notice_date"] = parse_date_series(masters.get("first_seen_notice_date", pd.Series(dtype=str)))
    masters["last_seen_notice_date"] = parse_date_series(masters.get("last_seen_notice_date", pd.Series(dtype=str)))
    masters["weeks_seen"] = pd.to_numeric(masters.get("weeks_seen", pd.Series(dtype=str)), errors="coerce")
    sources["notice_date"] = parse_date_series(sources.get("notice_date", pd.Series(dtype=str)))

    occurrences["line_key"] = occurrences.apply(extract_line_key, axis=1)
    occurrences["reason_group"] = occurrences["reason"].apply(normalise_reason)
    occurrences["segment_key"] = (
        occurrences["line_key"].fillna("UNKNOWN")
        + " | "
        + occurrences.get("distance_km", "").map(clean_text)
    )

    if "tsr_master_id" in occurrences.columns and "tsr_master_id" in masters.columns:
        occurrences_with_master = occurrences.merge(
            masters[["tsr_master_id", "weeks_seen"]],
            on="tsr_master_id",
            how="left",
            suffixes=("", "_master"),
        )
    else:
        occurrences_with_master = occurrences.copy()
        occurrences_with_master["weeks_seen"] = pd.NA

    occurrences_with_master["weeks_seen"] = pd.to_numeric(occurrences_with_master.get("weeks_seen", pd.Series(dtype=str)), errors="coerce")

    # 1. Line/month summary.
    line_month = (
        occurrences_with_master.groupby(["year", "month", "year_month", "line_key"], dropna=False)
        .agg(
            restriction_occurrences=("tsr_record_id", "count"),
            distinct_tsr_count=("tsr_master_id", pd.Series.nunique),
            average_weeks_seen=("weeks_seen", "mean"),
            median_weeks_seen=("weeks_seen", median_or_blank),
            max_weeks_seen=("weeks_seen", "max"),
            total_tsr_weeks=("weeks_seen", "sum"),
        )
        .reset_index()
    )
    line_month.to_csv(analytics_dir / "pta_tsr_line_month_summary.csv", index=False, encoding="utf-8-sig")

    # 2. Reason summary.
    reason_summary = (
        occurrences_with_master.groupby(["reason_group", "reason"], dropna=False)
        .agg(
            restriction_occurrences=("tsr_record_id", "count"),
            distinct_tsr_count=("tsr_master_id", pd.Series.nunique),
            average_weeks_seen=("weeks_seen", "mean"),
            median_weeks_seen=("weeks_seen", median_or_blank),
            min_weeks_seen=("weeks_seen", "min"),
            max_weeks_seen=("weeks_seen", "max"),
            total_tsr_weeks=("weeks_seen", "sum"),
        )
        .reset_index()
        .rename(columns={"reason": "reason_raw"})
    )
    reason_summary.to_csv(analytics_dir / "pta_tsr_reason_summary.csv", index=False, encoding="utf-8-sig")

    # 3. Segment summary.
    segment_summary = (
        occurrences_with_master.groupby(["line_key", "line_section", "location", "distance_km", "segment_key"], dropna=False)
        .agg(
            restriction_occurrences=("tsr_record_id", "count"),
            distinct_tsr_count=("tsr_master_id", pd.Series.nunique),
            average_weeks_seen=("weeks_seen", "mean"),
            median_weeks_seen=("weeks_seen", median_or_blank),
            max_weeks_seen=("weeks_seen", "max"),
            total_tsr_weeks=("weeks_seen", "sum"),
            first_seen_notice_date=("notice_date", "min"),
            last_seen_notice_date=("notice_date", "max"),
            latest_reason=("reason", "last"),
        )
        .reset_index()
    )
    segment_summary.to_csv(analytics_dir / "pta_tsr_segment_summary.csv", index=False, encoding="utf-8-sig")

    # 4. Active by notice.
    notice_master = occurrences.sort_values(["notice_date", "tsr_master_id"]).copy()
    distinct_notice_dates = sorted(notice_master["notice_date"].dropna().unique())
    active_rows = []
    previous_set: set[str] = set()
    for notice_date in distinct_notice_dates:
        current = set(notice_master.loc[notice_master["notice_date"] == notice_date, "tsr_master_id"].astype(str))
        new = current - previous_set
        continuing = current & previous_set
        closed = previous_set - current
        frame = notice_master[notice_master["notice_date"] == notice_date]
        for line_key, line_frame in frame.groupby("line_key"):
            line_current = set(line_frame["tsr_master_id"].astype(str))
            active_rows.append(
                {
                    "notice_date": notice_date,
                    "year": pd.Timestamp(notice_date).year,
                    "month": pd.Timestamp(notice_date).month,
                    "year_month": pd.Timestamp(notice_date).strftime("%Y-%m"),
                    "line_key": line_key,
                    "active_tsr_count": len(line_current),
                    "new_tsr_count": len(line_current & new),
                    "continuing_tsr_count": len(line_current & continuing),
                    "closed_since_previous_notice_count": len(closed),
                }
            )
        previous_set = current
    active_by_notice = pd.DataFrame(active_rows)
    active_by_notice.to_csv(analytics_dir / "pta_tsr_active_by_notice.csv", index=False, encoding="utf-8-sig")

    # 5. Duration buckets.
    latest_notice = occurrences["notice_date"].max()
    masters_duration = masters.copy()
    masters_duration["line_key"] = masters_duration.apply(extract_line_key, axis=1)
    masters_duration["duration_bucket"] = masters_duration["weeks_seen"].apply(duration_bucket)
    masters_duration["is_active_latest_notice"] = masters_duration["last_seen_notice_date"].eq(latest_notice)
    masters_duration.to_csv(analytics_dir / "pta_tsr_duration_buckets.csv", index=False, encoding="utf-8-sig")

    # 6. Data quality summary.
    status_counts = sources["status"].value_counts(dropna=False).to_dict() if "status" in sources.columns else {}
    data_quality = pd.DataFrame(
        [
            {"metric": "Total discovered PDFs", "value": len(sources), "notes": "Rows in pta_tsr_source_pdfs.csv"},
            {"metric": "Processed PDFs", "value": status_counts.get("processed", 0), "notes": "Source PDFs successfully processed"},
            {"metric": "Missing PDFs", "value": status_counts.get("missing", 0), "notes": "PTA API listed the item, but the URL could not be downloaded"},
            {"metric": "Failed PDFs", "value": status_counts.get("failed", 0), "notes": "Script or extraction failure"},
            {"metric": "Total TSR occurrences", "value": len(occurrences), "notes": "Rows in pta_tsr_occurrences.csv"},
            {"metric": "Total TSR masters", "value": len(masters), "notes": "Rows in pta_tsr_masters.csv"},
            {"metric": "First notice date", "value": occurrences["notice_date"].min(), "notes": "Earliest processed notice date"},
            {"metric": "Last notice date", "value": occurrences["notice_date"].max(), "notes": "Latest processed notice date"},
        ]
    )
    data_quality.to_csv(analytics_dir / "pta_tsr_data_quality_summary.csv", index=False, encoding="utf-8-sig")

    print(f"Analytics CSVs written to: {analytics_dir.resolve()}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate PTA TSR analytics summary CSVs for Power BI.")
    parser.add_argument("--exports-dir", type=Path, default=DEFAULT_EXPORTS_DIR)
    parser.add_argument("--analytics-dir", type=Path, default=DEFAULT_ANALYTICS_DIR)
    args = parser.parse_args()
    create_analytics(args.exports_dir, args.analytics_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
