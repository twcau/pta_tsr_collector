# PTA TSR Collector - Power BI DAX Measures

Create these measures in Power BI Desktop using **Modeling > New measure**.

Recommended home table: create a small table named `Measures` using **Home > Enter data** with one dummy column and one dummy row, then hide the dummy column. Put all measures in that table.

## Core count measures

```DAX
Total TSR Occurrences =
COUNTROWS ( Occurrences )
```

```DAX
Distinct TSRs =
DISTINCTCOUNT ( Occurrences[tsr_master_id] )
```

```DAX
Total Source PDFs =
COUNTROWS ( 'Source PDFs' )
```

```DAX
Processed PDFs =
CALCULATE (
    COUNTROWS ( 'Source PDFs' ),
    'Source PDFs'[status] = "processed"
)
```

```DAX
Missing PDFs =
CALCULATE (
    COUNTROWS ( 'Source PDFs' ),
    'Source PDFs'[status] = "missing"
)
```

```DAX
Failed PDFs =
CALCULATE (
    COUNTROWS ( 'Source PDFs' ),
    'Source PDFs'[status] = "failed"
)
```

```DAX
PDF Processing Coverage =
DIVIDE ( [Processed PDFs], [Total Source PDFs] )
```

## Duration measures

```DAX
Average Weeks Seen =
AVERAGE ( Masters[weeks_seen] )
```

```DAX
Median Weeks Seen =
MEDIAN ( Masters[weeks_seen] )
```

```DAX
Longest TSR Weeks =
MAX ( Masters[weeks_seen] )
```

```DAX
Total TSR Weeks =
SUM ( 'Duration Buckets'[weeks_seen] )
```

```DAX
Active Long Running TSRs =
CALCULATE (
    DISTINCTCOUNT ( 'Duration Buckets'[tsr_master_id] ),
    'Duration Buckets'[is_active_latest_notice] = TRUE (),
    'Duration Buckets'[weeks_seen] >= 52
)
```

## Active-by-notice measures

```DAX
Active TSRs =
SUM ( 'Active By Notice'[active_tsr_count] )
```

```DAX
New TSRs =
SUM ( 'Active By Notice'[new_tsr_count] )
```

```DAX
Continuing TSRs =
SUM ( 'Active By Notice'[continuing_tsr_count] )
```

```DAX
Closed Since Previous Notice =
SUM ( 'Active By Notice'[closed_since_previous_notice_count] )
```

## Segment/reason measures

```DAX
Total Segment TSR Weeks =
SUM ( 'Segment Summary'[total_tsr_weeks] )
```

```DAX
Average Segment Weeks Seen =
AVERAGE ( 'Segment Summary'[average_weeks_seen] )
```

```DAX
Total Reason TSR Weeks =
SUM ( 'Reason Summary'[total_tsr_weeks] )
```

```DAX
Average Reason Weeks Seen =
AVERAGE ( 'Reason Summary'[average_weeks_seen] )
```
