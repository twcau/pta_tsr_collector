# PTA TSR Collector - Power BI Build Guide

This guide assumes you have already imported the nine CSV data sources from the GitHub raw URLs.

The correct raw URL prefix is:

```text
https://raw.githubusercontent.com/twcau/pta_tsr_collector/refs/heads/main/
```

## 1. Rename tables

In Power BI Desktop, rename the imported tables exactly as follows:

```text
pta_tsr_occurrences.csv              -> Occurrences
pta_tsr_masters.csv                  -> Masters
pta_tsr_source_pdfs.csv              -> Source PDFs
pta_tsr_line_month_summary.csv       -> Line Month Summary
pta_tsr_reason_summary.csv           -> Reason Summary
pta_tsr_segment_summary.csv          -> Segment Summary
pta_tsr_active_by_notice.csv         -> Active By Notice
pta_tsr_duration_buckets.csv         -> Duration Buckets
pta_tsr_data_quality_summary.csv     -> Data Quality Summary
```

Exact table names matter because the provided DAX measures assume these names.

## 2. Set data types

Open **Transform data** and set these data types.

### Occurrences

```text
tsr_record_id           Whole number
tsr_master_id           Whole number
notice_date             Date
source_page             Whole number
source_row_number       Whole number
```

Leave these as text:

```text
location
line_section
distance_km
stn_no
max_speed
date_imposed
reason
date_cancelled
source_pdf
source_url
```

### Masters

```text
tsr_master_id               Whole number
first_seen_notice_date      Date
last_seen_notice_date       Date
weeks_seen                  Decimal number
```

Leave descriptive/latest columns as text.

### Source PDFs

```text
source_pdf_id       Whole number
notice_date         Date
tsr_table_count     Whole number
tsr_row_count       Whole number
```

Leave `filename`, `url`, `status`, and `last_error` as text.

### Line Month Summary

```text
year                        Whole number
month                       Whole number
restriction_occurrences     Whole number
distinct_tsr_count          Whole number
average_weeks_seen          Decimal number
median_weeks_seen           Decimal number
max_weeks_seen              Decimal number
total_tsr_weeks             Decimal number
```

Leave `year_month` and `line_key` as text.

### Reason Summary

```text
restriction_occurrences     Whole number
distinct_tsr_count          Whole number
average_weeks_seen          Decimal number
median_weeks_seen           Decimal number
min_weeks_seen              Decimal number
max_weeks_seen              Decimal number
total_tsr_weeks             Decimal number
```

Leave `reason_group` and `reason_raw` as text.

### Segment Summary

```text
restriction_occurrences     Whole number
distinct_tsr_count          Whole number
average_weeks_seen          Decimal number
median_weeks_seen           Decimal number
max_weeks_seen              Decimal number
total_tsr_weeks             Decimal number
first_seen_notice_date      Date
last_seen_notice_date       Date
```

Leave location/segment fields as text.

### Active By Notice

```text
notice_date                             Date
year                                    Whole number
month                                   Whole number
active_tsr_count                        Whole number
new_tsr_count                           Whole number
continuing_tsr_count                    Whole number
closed_since_previous_notice_count      Whole number
```

Leave `year_month` and `line_key` as text.

### Duration Buckets

```text
tsr_master_id               Whole number
first_seen_notice_date      Date
last_seen_notice_date       Date
weeks_seen                  Decimal number
is_active_latest_notice     True/False
```

Leave descriptive fields and `duration_bucket` as text.

## 3. Create measures

1. Go to **Home > Enter data**.
2. Create a one-column, one-row table:

```text
Dummy
1
```

3. Name the table:

```text
Measures
```

4. In the Model/Data pane, hide the `Dummy` column.
5. Go to **Modeling > New measure**.
6. Copy each measure from `pta_tsr_powerbi_dax_measures.updated.md`.
7. After creating measures, set formatting:
   - `PDF Processing Coverage` -> Percentage, 1 decimal place
   - week/duration averages -> Decimal number, 1 decimal place
   - counts -> Whole number

## 4. Create relationships

In **Model view**, create these relationships:

```text
Occurrences[tsr_master_id]      -> Masters[tsr_master_id]
Occurrences[tsr_master_id]      -> Duration Buckets[tsr_master_id]
Occurrences[source_pdf]         -> Source PDFs[filename]
```

If Power BI warns about ambiguity, only keep:

```text
Occurrences[tsr_master_id] -> Masters[tsr_master_id]
```

The summary tables can safely operate independently for visuals.

## 5. Import the theme

1. Go to **View > Browse for themes**.
2. Select:

```text
pta_tsr_powerbi_theme.json
```

## 6. Build report pages

### Page 1 - Executive Overview

Page name:

```text
Executive Overview
```

Add slicers:

```text
Line Month Summary[year]
Line Month Summary[line_key]
Reason Summary[reason_group]
```

Add card visuals:

```text
Total TSR Occurrences
Distinct TSRs
Processed PDFs
Missing PDFs
PDF Processing Coverage
Longest TSR Weeks
```

Add line chart:

```text
Title: Active TSRs over time
X-axis: Active By Notice[notice_date]
Y-axis: Active TSRs
Legend: Active By Notice[line_key]
```

Add clustered bar chart:

```text
Title: Distinct TSRs by line
Y-axis: Line Month Summary[line_key]
X-axis: Line Month Summary[distinct_tsr_count]
Sort: distinct_tsr_count descending
```

Add clustered bar chart:

```text
Title: Restrictions by reason group
Y-axis: Reason Summary[reason_group]
X-axis: Reason Summary[restriction_occurrences]
Sort: restriction_occurrences descending
```

Add table:

```text
Title: Longest running TSRs
Fields:
  Duration Buckets[tsr_master_id]
  Duration Buckets[line_key]
  Duration Buckets[latest_location]
  Duration Buckets[latest_distance_km]
  Duration Buckets[latest_reason]
  Duration Buckets[first_seen_notice_date]
  Duration Buckets[last_seen_notice_date]
  Duration Buckets[weeks_seen]
Sort: weeks_seen descending
Visual-level filter: Top N = 20 by weeks_seen
```

### Page 2 - Line Performance

Page name:

```text
Line Performance
```

Add slicers:

```text
Line Month Summary[year]
Line Month Summary[line_key]
```

Add clustered column chart:

```text
Title: Restrictions by line and month
X-axis: Line Month Summary[year_month]
Y-axis: Line Month Summary[restriction_occurrences]
Legend: Line Month Summary[line_key]
```

Add matrix:

```text
Title: Monthly restrictions by line
Rows: Line Month Summary[line_key]
Columns: Line Month Summary[year_month]
Values: Line Month Summary[restriction_occurrences]
Conditional formatting: background colour by value
```

Add table:

```text
Title: Worst segments by total TSR-weeks
Fields:
  Segment Summary[line_key]
  Segment Summary[line_section]
  Segment Summary[location]
  Segment Summary[distance_km]
  Segment Summary[restriction_occurrences]
  Segment Summary[distinct_tsr_count]
  Segment Summary[average_weeks_seen]
  Segment Summary[max_weeks_seen]
  Segment Summary[total_tsr_weeks]
Sort: total_tsr_weeks descending
Visual-level filter: Top N = 25 by total_tsr_weeks
```

Add scatter chart:

```text
Title: Frequency vs duration by segment
X-axis: Segment Summary[distinct_tsr_count]
Y-axis: Segment Summary[average_weeks_seen]
Size: Segment Summary[total_tsr_weeks]
Legend: Segment Summary[line_key]
Details: Segment Summary[segment_key]
```

### Page 3 - Restriction Type / Reason

Page name:

```text
Restriction Type
```

Add slicers:

```text
Reason Summary[reason_group]
```

Add bar chart:

```text
Title: Most common restriction types
Y-axis: Reason Summary[reason_group]
X-axis: Reason Summary[restriction_occurrences]
Sort: restriction_occurrences descending
```

Add bar chart:

```text
Title: Average weeks seen by restriction type
Y-axis: Reason Summary[reason_group]
X-axis: Reason Summary[average_weeks_seen]
Sort: average_weeks_seen descending
```

Add table:

```text
Title: Raw restriction reasons
Fields:
  Reason Summary[reason_group]
  Reason Summary[reason_raw]
  Reason Summary[restriction_occurrences]
  Reason Summary[distinct_tsr_count]
  Reason Summary[average_weeks_seen]
  Reason Summary[max_weeks_seen]
  Reason Summary[total_tsr_weeks]
Sort: restriction_occurrences descending
```

### Page 4 - Duration and Ageing

Page name:

```text
Duration and Ageing
```

Add slicers:

```text
Duration Buckets[line_key]
Duration Buckets[duration_bucket]
Duration Buckets[is_active_latest_notice]
```

Add column chart:

```text
Title: TSR duration buckets
X-axis: Duration Buckets[duration_bucket]
Y-axis: count of Duration Buckets[tsr_master_id]
```

Sort duration buckets manually in this order:

```text
1 week
2-4 weeks
1-3 months
3-6 months
6-12 months
1-2 years
2-5 years
5+ years
Unknown
```

Add cards:

```text
Average Weeks Seen
Median Weeks Seen
Longest TSR Weeks
Active Long Running TSRs
```

Add table:

```text
Title: Long-running active TSRs
Fields:
  Duration Buckets[tsr_master_id]
  Duration Buckets[line_key]
  Duration Buckets[latest_location]
  Duration Buckets[latest_distance_km]
  Duration Buckets[latest_reason]
  Duration Buckets[weeks_seen]
  Duration Buckets[duration_bucket]
  Duration Buckets[is_active_latest_notice]
Visual-level filters:
  is_active_latest_notice = True
  weeks_seen >= 52
Sort: weeks_seen descending
```

### Page 5 - Data Quality

Page name:

```text
Data Quality
```

Add card visuals:

```text
Total Source PDFs
Processed PDFs
Missing PDFs
Failed PDFs
PDF Processing Coverage
```

Add table:

```text
Title: Data quality metrics
Fields:
  Data Quality Summary[metric]
  Data Quality Summary[value]
  Data Quality Summary[notes]
```

Add table:

```text
Title: Missing source PDFs
Fields:
  Source PDFs[notice_date]
  Source PDFs[filename]
  Source PDFs[url]
  Source PDFs[last_error]
Visual-level filter:
  Source PDFs[status] = missing
```

## 7. Publish to Power BI Service

1. Save the PBIX file.
2. Select **Home > Publish**.
3. Publish to your workspace.
4. In Power BI Service, open the semantic model settings.
5. Set web source credentials to Anonymous if prompted.
6. Configure scheduled refresh.
7. Open the report and use **File > Embed report > Publish to web (public)** only when ready for public release.
