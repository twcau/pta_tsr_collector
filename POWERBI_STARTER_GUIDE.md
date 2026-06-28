# PTA TSR Collector Power BI Starter Kit

This folder provides the fastest practical path to a public Power BI dashboard backed by the GitHub-hosted CSV files from:

```text
https://github.com/twcau/pta_tsr_collector
```

- [What is included](#what-is-included)
- [Step 1 - Generate analytics CSVs](#step-1---generate-analytics-csvs)
- [Step 2 - Commit outputs to GitHub](#step-2---commit-outputs-to-github)
- [Step 3 - Load data in Power BI Desktop](#step-3---load-data-in-power-bi-desktop)
- [Step 4 - Report pages to create](#step-4---report-pages-to-create)
  - [Executive overview](#executive-overview)
  - [Line performance](#line-performance)
  - [Restriction type / reason](#restriction-type--reason)
  - [Duration and ageing](#duration-and-ageing)
  - [Data quality](#data-quality)
- [Step 5 - Publish publicly](#step-5---publish-publicly)

## What is included

```text
pta_tsr_generate_analytics.py
pta_tsr_powerbi_queries.pq
pta_tsr_powerbi_dax_measures.md
```

## Step 1 - Generate analytics CSVs

From the repository root:

```powershell
py .\pta_tsr_generate_analytics.py
```

This creates:

```text
pta_tsr_data\analytics\pta_tsr_line_month_summary.csv
pta_tsr_data\analytics\pta_tsr_reason_summary.csv
pta_tsr_data\analytics\pta_tsr_segment_summary.csv
pta_tsr_data\analytics\pta_tsr_active_by_notice.csv
pta_tsr_data\analytics\pta_tsr_duration_buckets.csv
pta_tsr_data\analytics\pta_tsr_data_quality_summary.csv
```

## Step 2 - Commit outputs to GitHub

```powershell
git add pta_tsr_data/exports/*.csv
git add pta_tsr_data/analytics/*.csv
git add pta_tsr_generate_analytics.py
git add pta_tsr_powerbi_queries.pq
git add pta_tsr_powerbi_dax_measures.md
git commit -m "Add Power BI analytics exports"
git push
```

## Step 3 - Load data in Power BI Desktop

Use **Get data > Web** and load these raw GitHub URLs:

```text
https://raw.githubusercontent.com/twcau/pta_tsr_collector/main/pta_tsr_data/exports/pta_tsr_occurrences.csv
https://raw.githubusercontent.com/twcau/pta_tsr_collector/main/pta_tsr_data/exports/pta_tsr_masters.csv
https://raw.githubusercontent.com/twcau/pta_tsr_collector/main/pta_tsr_data/exports/pta_tsr_source_pdfs.csv
https://raw.githubusercontent.com/twcau/pta_tsr_collector/main/pta_tsr_data/analytics/pta_tsr_line_month_summary.csv
https://raw.githubusercontent.com/twcau/pta_tsr_collector/main/pta_tsr_data/analytics/pta_tsr_reason_summary.csv
https://raw.githubusercontent.com/twcau/pta_tsr_collector/main/pta_tsr_data/analytics/pta_tsr_segment_summary.csv
https://raw.githubusercontent.com/twcau/pta_tsr_collector/main/pta_tsr_data/analytics/pta_tsr_active_by_notice.csv
https://raw.githubusercontent.com/twcau/pta_tsr_collector/main/pta_tsr_data/analytics/pta_tsr_duration_buckets.csv
https://raw.githubusercontent.com/twcau/pta_tsr_collector/main/pta_tsr_data/analytics/pta_tsr_data_quality_summary.csv
```

## Step 4 - Report pages to create

### Executive overview

- Cards: Total TSR Occurrences, Distinct TSRs, Processed PDFs, Missing PDFs, PDF Processing Coverage
- Line chart: active TSR count by notice date
- Bar chart: distinct TSRs by line
- Bar chart: TSRs by reason group
- Table: longest-running TSRs from Duration Buckets

### Line performance

- Bar chart: restrictions by line
- Matrix: line by year_month
- Table: worst segments by total_tsr_weeks
- Scatter: distinct_tsr_count vs average_weeks_seen, size total_tsr_weeks

### Restriction type / reason

- Bar chart: reason_group by restriction_occurrences
- Bar chart: reason_group by average_weeks_seen
- Table: reason_raw with max_weeks_seen and total_tsr_weeks

### Duration and ageing

- Column chart: duration_bucket count
- Table: 1+ year or 5+ year restrictions
- KPI: Longest TSR Weeks
- KPI: Average Weeks Seen

### Data quality

- Table: Data Quality Summary
- Table: missing PDFs from Source PDFs where status = missing

## Step 5 - Publish publicly

Publish the report to Power BI Service, then use:

```text
File > Embed report > Publish to web (public)
```

Only do this once you are comfortable that all data in the model is public.
