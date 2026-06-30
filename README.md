# PTA TSR Collector

A Python-based collection and analysis pipeline for extracting **Temporary Speed Restriction (TSR)** data from Public Transport Authority of Western Australia (PTA) Weekly Notices.

The project discovers PTA Weekly Notice PDFs, downloads them, extracts the **Current Temporary Speed Restrictions / Current Speed Restrictions** table from each notice, stores the results in a resumable SQLite database, assigns recurring restrictions to master TSR records, and exports analysis-ready CSV files.

> [!TIP] > **Eventual project goal**
> A PowerBI view that includes a map overlay of Perth's rail network, to see repeat hotspots for speed restrictions and maintenance issues.

> [!NOTE]
>
> **Project status**: Early operational / exploratory. The collector has successfully discovered a non-zero corpus of PTA Weekly Notice PDFs via the PTA DNN Document Viewer API, and has begun processing real historical PDF notices into a CSV file. Further tuning of the data extracted to ensure consistent and clean information is underway, before making a PowerBI view available to start exploring the data set.

## Table of contents

- [Why this project exists](#why-this-project-exists)
- [What the collector does](#what-the-collector-does)
- [What is unique about this project](#what-is-unique-about-this-project)
  - [PTA document browser support](#pta-document-browser-support)
  - [Mixed historical folder structures](#mixed-historical-folder-structures)
  - [Mixed historical filename formats](#mixed-historical-filename-formats)
  - [Mixed historical table layouts](#mixed-historical-table-layouts)
  - [Location and distance splitting](#location-and-distance-splitting)
  - [TSR master matching](#tsr-master-matching)
- [Repository contents](#repository-contents)
- [Requirements](#requirements)
- [Windows quick start](#windows-quick-start)
  - [1. Install Python](#1-install-python)
  - [2. Confirm Python works](#2-confirm-python-works)
  - [3. Create the project folder](#3-create-the-project-folder)
  - [4. Discover PDFs](#4-discover-pdfs)
  - [5. Check status](#5-check-status)
  - [6. Process a small test batch](#6-process-a-small-test-batch)
  - [7. Process the full archive](#7-process-the-full-archive)
- [Common commands](#common-commands)
  - [Initialise database only](#initialise-database-only)
  - [Discover PDFs only](#discover-pdfs-only)
  - [Process pending PDFs only](#process-pending-pdfs-only)
  - [Retry failed or missing PDFs](#retry-failed-or-missing-pdfs)
  - [Limit processing to a small batch](#limit-processing-to-a-small-batch)
  - [Full discovery, processing and export run](#full-discovery-processing-and-export-run)
  - [Export CSVs again](#export-csvs-again)
  - [Show current status](#show-current-status)
  - [Create diagnostics ZIP](#create-diagnostics-zip)
- [Output files](#output-files)
  - [`pta_tsr_occurrences.csv`](#pta_tsr_occurrencescsv)
  - [`pta_tsr_masters.csv`](#pta_tsr_masterscsv)
  - [`pta_tsr_source_pdfs.csv`](#pta_tsr_source_pdfscsv)
- [Processing statuses](#processing-statuses)
- [Known real-world data issues](#known-real-world-data-issues)
- [Data model overview](#data-model-overview)
  - [`source_pdf`](#source_pdf)
  - [`tsr_occurrence`](#tsr_occurrence)
  - [`tsr_master`](#tsr_master)
- [Example analysis questions](#example-analysis-questions)
- [Using this project for other rail networks](#using-this-project-for-other-rail-networks)
- [Responsible use and interpretation](#responsible-use-and-interpretation)
- [Troubleshooting](#troubleshooting)
  - [`SyntaxWarning: invalid escape sequence`](#syntaxwarning-invalid-escape-sequence)
  - [`Found/registered 0 PDF link(s)`](#foundregistered-0-pdf-links)
  - [`404 Not Found` while downloading a PDF](#404-not-found-while-downloading-a-pdf)
  - [CSV files are empty](#csv-files-are-empty)
  - [A PDF fails table extraction](#a-pdf-fails-table-extraction)
- [Scheduling weekly updates on Windows](#scheduling-weekly-updates-on-windows)
- [Git ignore suggestions](#git-ignore-suggestions)
- [Limitations](#limitations)
- [Roadmap ideas](#roadmap-ideas)
- [Licence and attribution](#licence-and-attribution)
- [Acknowledgement](#acknowledgement)

## Why this project exists

Temporary speed restrictions are operationally important because they can indicate infrastructure condition, defect remediation timelines, operational constraints, renewal backlogs, or recurring maintenance pressure points across a rail network.

The PTA publishes Weekly Notices containing safety and operational information. Embedded in those notices is a table of current speed restrictions. While each weekly notice is useful on its own, the real analytical value comes from collecting the table across many weeks and asking questions such as:

- Which TSRs have persisted the longest?
- Which corridors, line sections, or directions see repeated TSRs?
- Which restriction reasons appear most often?
- How often do restrictions become effectively permanent?
- How long does it take for different classes of rail infrastructure restrictions to be removed?
- Are some restriction types seasonal, recurring, or clustered?

However, the data is not published in a format that enables ease of analysis. Hence this Python script.

This script turns the temporary speed restrictions table contained within the weekly PDF notices into a longitudinal dataset that can support answering those kinds of questions.

Although this repository is tailored to the PTA's public document structure, the approach may be useful to anyone interested in analysing rail maintenance communications, operational notices, infrastructure performance signals, or public transport network impacts in another jurisdiction.

## What the collector does

At a high level, the collector:

- Connects to the PTA Safety Resources / Weekly Notices document browser.
- Uses the PTA site's DNN Document Viewer JSON API to recursively enumerate folders.
- Finds Weekly Notice PDFs.
- Registers discovered PDFs in a local SQLite database.
- Downloads each PDF only once.
- Searches each PDF for the speed restriction table.
- Extracts TSR rows from the table.
- Normalises key fields such as notice date, location, distance, speed and date imposed.
- Assigns every row a sequential occurrence ID.
- Groups repeated weekly appearances of the same TSR under a master TSR ID.
- Exports CSV files for spreadsheet, database, or BI analysis.

## What is unique about this project

This is not a generic PDF scraper. Several PTA-specific and domain-specific issues are handled deliberately.

### PTA document browser support

The PTA Safety Resources page does not expose Weekly Notice PDFs as ordinary static links in the initial HTML. The visible folder browser is driven by a DNN / DotNetNuke Document Viewer module.

The collector therefore uses the underlying API endpoint:

```text
https://www.pta.wa.gov.au/API/DocumentViewer/ContentService/GetFolderContent
```

The collector starts at the identified Weekly Notices folder ID:

```text
5160
```

and recursively walks child folder records returned by the API.

### Mixed historical folder structures

The PTA Weekly Notice archive is not completely uniform. Some years contain PDFs directly under the year folder. Other years are split into month folders.

The collector does not assume one fixed folder depth. It follows every folder returned by the API and extracts PDF records wherever they are found.

### Mixed historical filename formats

The notices use changing date styles, including examples such as:

```text
Weekly Notice No. 26 Week Commencing 28th June 2026.pdf
Weekly Notice No. 44 - Week Ending 10 Nov 2018.pdf
Weekly Notice No. 35 - Week Ending 8th September 2018.pdf
Weekly Notice No. 31 Week Ending 6 August2022.pdf
Weekly Notice No. 04 Week Ending- 01st Febuary 2019.pdf
```

The collector attempts to normalise these into:

```text
YYYY-MM-DD
```

### Mixed historical table layouts

The TSR table has changed over time. Older notices can use headings such as:

```text
Current Speed Restrictions
```

with columns like:

```text
Section of Railway
Distance at or between (km)
Maximum Speed
Reason for Restriction
Date Imposed
```

Later notices can include:

```text
Current Temporary Speed Restrictions (TSR)
```

with columns like:

```text
Location To and From (km)
STN No.
Maximum Speed
Date Imposed
Reason for Restriction
Date to be Cancelled
```

The collector uses flexible table detection and column mapping rather than assuming one exact table schema.

### Location and distance splitting

Current PTA notices may combine location, line code/direction, and kilometres into one field, for example:

```text
MTMDN 7.900km - 8.150km
Beyond Service 21.012km - 26.000km
```

The collector splits these into separate fields where possible:

```text
location
line_section
distance_km
```

This makes it easier to analyse restrictions by line, direction, section, or location.

### TSR master matching

Every appearance of a TSR in a weekly notice is stored as an occurrence. The collector also creates a master record for the underlying TSR.

The current master matching rule is:

```text
location + distance_km + stn_no + date_imposed
```

Speed is intentionally **not** part of the master identity. If a restriction changes from 80 km/h to 60 km/h, that is treated as a modification of the same TSR, not a brand-new TSR.

## Repository contents

Suggested repository structure:

```text
pta-tsr-collector/
  README.md
  pta_tsr_collector.py
  .gitignore
```

Generated runtime data is intentionally kept out of source control:

```text
pta_tsr_data/
  pta_tsr.sqlite3
  pdfs/
  exports/
  logs/
  diagnostics/
```

## Requirements

- Windows, macOS, or Linux
- Python 3.11 or newer recommended
- Internet access to the PTA website
- Python packages installed by the script when `--install-deps` is used:
  - `requests`
  - `beautifulsoup4`
  - `pdfplumber`

The project has been developed and tested primarily from Windows PowerShell.

## Windows quick start

### 1. Install Python

Download Python from:

```text
https://www.python.org/downloads/windows/
```

During installation, tick:

```text
Add python.exe to PATH
```

### 2. Confirm Python works

Open PowerShell and run:

```powershell
py --version
```

If `py` is not available, try:

```powershell
python --version
```

### 3. Create the project folder

```powershell
mkdir C:\PythonScripts\pta_tsr_collector
cd C:\PythonScripts\pta_tsr_collector
```

Place the script here:

```text
C:\PythonScripts\pta_tsr_collector\pta_tsr_collector.py
```

### 4. Discover PDFs

```powershell
py .\pta_tsr_collector.py discover --folder-id 5160 --install-deps
```

This should populate the local database with discovered Weekly Notice PDFs.

### 5. Check status

```powershell
py .\pta_tsr_collector.py status
```

You should see a non-zero PDF count.

### 6. Process a small test batch

```powershell
py .\pta_tsr_collector.py process --limit 5
```

This is a safer first processing test before running across the full archive.

### 7. Process the full archive

```powershell
py .\pta_tsr_collector.py run --retry-failed
```

## Common commands

### Initialise database only

```powershell
py .\pta_tsr_collector.py init
```

### Discover PDFs only

```powershell
py .\pta_tsr_collector.py discover --folder-id 5160
```

### Process pending PDFs only

```powershell
py .\pta_tsr_collector.py process
```

### Retry failed or missing PDFs

```powershell
py .\pta_tsr_collector.py process --retry-failed
```

### Limit processing to a small batch

```powershell
py .\pta_tsr_collector.py process --limit 10
```

### Full discovery, processing and export run

```powershell
py .\pta_tsr_collector.py run --retry-failed
```

### Export CSVs again

```powershell
py .\pta_tsr_collector.py export
```

### Show current status

```powershell
py .\pta_tsr_collector.py status
```

### Create diagnostics ZIP

```powershell
py .\pta_tsr_collector.py diagnostics
```

## Output files

After running, the collector creates:

```text
pta_tsr_data/
  pta_tsr.sqlite3
  pdfs/
  exports/
    pta_tsr_occurrences.csv
    pta_tsr_masters.csv
    pta_tsr_source_pdfs.csv
  logs/
    pta_tsr_collector.log
  diagnostics/
    dnn_folder_<folder_id>.json
```

### `pta_tsr_occurrences.csv`

One row per TSR appearance in a Weekly Notice.

Important fields include:

```text
tsr_record_id
tsr_master_id
notice_date
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
source_page
source_row_number
```

### `pta_tsr_masters.csv`

One row per underlying TSR after grouping repeated appearances across notices.

Important fields include:

```text
tsr_master_id
first_seen_notice_date
last_seen_notice_date
weeks_seen
latest_location
latest_line_section
latest_distance_km
latest_stn_no
latest_max_speed
date_imposed
latest_reason
latest_date_cancelled
master_fingerprint
```

### `pta_tsr_source_pdfs.csv`

Audit file for source PDF discovery and processing state.

Important fields include:

```text
source_pdf_id
notice_date
filename
url
status
tsr_table_count
tsr_row_count
last_error
```

## Processing statuses

The `source_pdf` table and `pta_tsr_source_pdfs.csv` use statuses such as:

```text
discovered
downloaded
processed
failed
missing
```

Meaning:

- `discovered` — the PDF was found in the PTA document browser but has not yet been processed.
- `downloaded` — the PDF was downloaded but not fully processed.
- `processed` — the PDF was downloaded, scanned and exported successfully.
- `failed` — the script encountered an error while processing the PDF.
- `missing` — the PTA API listed the PDF, but the file URL could not be downloaded, usually because of a stale or malformed PTA record.

## Known real-world data issues

This project exists because the source material is useful but not analysis-ready. Known issues include:

- Historical notices have inconsistent folder structures.
- Historical notices have inconsistent file naming.
- Some filenames contain typographical errors such as `Febuary`.
- Some filenames omit spaces, such as `August2022`.
- Some API-listed documents may return `404 Not Found` when downloaded.
- Table structures changed between earlier and later years.
- Some older tables do not include all newer fields, such as STN number or cancellation date.
- PDF table extraction can be affected by page layout, merged cells, repeated headers or subtle PDF formatting changes.

The collector is designed to continue processing despite these issues and to record diagnostics for later review.

## Data model overview

The collector uses a local SQLite database with three main tables.

### `source_pdf`

Tracks every Weekly Notice PDF discovered from the PTA site.

### `tsr_occurrence`

Stores every TSR row extracted from every Weekly Notice.

This is the most detailed table and is the best starting point for longitudinal analysis.

### `tsr_master`

Stores grouped TSR identities across weeks.

This table supports duration analysis, such as first seen, last seen and weeks seen.

## Example analysis questions

Once data has been collected, possible questions include:

- Which TSRs have existed for the most weeks?
- Which locations have the highest number of TSR occurrences?
- Which restriction reasons are most common?
- How many TSRs are listed as indefinite, permanent, TBA, TBC or TBD?
- Which restrictions change speed while remaining active?
- Which restrictions disappeared from notices after a planned cancellation date?
- Which lines or sections have recurring restrictions?
- Are restrictions clustered around particular kilometre ranges?
- How does the number of active restrictions change over time?

## Using this project for other rail networks

This project is PTA-specific in its discovery layer but more general in its concept.

If another operator publishes weekly or periodic notices containing speed restriction tables, the same pattern may work:

- Identify where documents are published.
- Determine whether the document index is static HTML, a JavaScript app, an API, SharePoint, an S3 bucket, or another document system.
- Build a reliable document discovery layer.
- Download documents to a local cache.
- Extract the relevant table.
- Store raw occurrences separately from grouped master records.
- Export repeatable CSVs for analysis.

The most important architectural lesson is to separate:

```text
document discovery
PDF download/cache
PDF table extraction
normalisation
master matching
CSV/report export
```

That separation makes the project easier to adapt when a website changes or when historical document formats vary.

## Responsible use and interpretation

This project extracts data from public documents for analysis. The dataset should still be interpreted carefully.

A TSR appearing for a long period does not, by itself, prove neglect, poor maintenance, or unsafe operation. Long-running restrictions can result from many causes, including planned staged works, funding cycles, engineering constraints, access windows, risk controls, asset renewals, environmental conditions, or operational decisions.

The dataset is best treated as a structured evidence base for further investigation, not as a final conclusion.

Recommended practice:

- keep source URLs and source PDFs for traceability;
- distinguish missing data from genuine absence of restrictions;
- review outliers manually against the original PDF;
- avoid over-interpreting one field without operational context;
- document any assumptions used in master matching or duration calculations.

## Troubleshooting

### `SyntaxWarning: invalid escape sequence`

Older versions of the script contained Windows paths in normal Python string literals. Version 2.2 uses raw strings for these help/docstring sections.

### `Found/registered 0 PDF link(s)`

This means generic HTML crawling did not find PDFs. The PTA page uses a DNN Document Viewer API, so use:

```powershell
py .\pta_tsr_collector.py discover --folder-id 5160
```

### `404 Not Found` while downloading a PDF

Some PTA API records may point to stale or malformed file URLs. Version 2.2 tries conservative alternate URLs and then marks the record as `missing` if the PDF still cannot be downloaded.

### CSV files are empty

Check status:

```powershell
py .\pta_tsr_collector.py status
```

If PDFs are still discovered, run:

```powershell
py .\pta_tsr_collector.py process --limit 5
```

### A PDF fails table extraction

Create diagnostics:

```powershell
py .\pta_tsr_collector.py diagnostics
```

Then inspect:

```text
pta_tsr_data\logs\pta_tsr_collector.log
pta_tsr_data\exports\pta_tsr_source_pdfs.csv
pta_tsr_data\diagnostics\
```

## Scheduling weekly updates on Windows

Once the full historical backfill is complete, the same script can be scheduled.

Suggested Windows Task Scheduler configuration:

- Program/script:

```text
py
```

- Arguments:

```text
C:\PythonScripts\pta_tsr_collector\pta_tsr_collector.py run --retry-failed
```

- Start in:

```text
C:\PythonScripts\pta_tsr_collector
```

Schedule the task after the weekly notice is normally published.

## Git ignore suggestions

Generated data should usually not be committed:

```gitignore
pta_tsr_data/
__pycache__/
*.pyc
.env
.venv/
```

If you want to publish a sample dataset, consider creating a separate curated `samples/` folder with small, documented extracts rather than committing the full local database and downloaded PDFs.

## Limitations

- The collector depends on the current PTA DNN Document Viewer API behaviour.
- Historical PDFs may contain formatting changes not yet handled by the parser.
- Automated table extraction may require further tuning after reviewing all historical notices.
- Master matching is heuristic and should be reviewed before using outputs for formal conclusions.
- The project does not yet include a Power BI dashboard or interactive reporting layer.

## Roadmap ideas

Potential future improvements:

- stronger validation of extracted rows;
- improved parsing of line codes and directions;
- explicit active/inactive TSR lifecycle table;
- automatic weekly change reports;
- Power BI or DuckDB-ready model;
- anomaly detection for unusually long-running TSRs;
- HTML or Markdown summary reports;
- unit tests with sampled historical PDFs;
- GitHub Actions linting and smoke tests;
- configuration file support for adapting the collector to other networks.

## Licence and attribution

This project is an independent data extraction and analysis tool. It is not affiliated with, endorsed by, or maintained by the Public Transport Authority of Western Australia.

Users are responsible for complying with applicable website terms, copyright rules, public-sector information reuse requirements, and any local policies that apply to their use case.

## Acknowledgement

This project was created to make public operational notice data easier to analyse over time. The aim is to support transparent, evidence-based discussion about rail infrastructure condition, maintenance impacts, and operational restrictions.
