# PTA TSR Collector - Power BI DAX Measures

Create these measures in Power BI Desktop after loading the CSVs.

```DAX
Total TSR Occurrences =
COUNTROWS ( Occurrences )
```

```DAX
Distinct TSRs =
DISTINCTCOUNT ( Occurrences[tsr_master_id] )
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
DIVIDE ( [Processed PDFs], COUNTROWS ( 'Source PDFs' ) )
```

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
