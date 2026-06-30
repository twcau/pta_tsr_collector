# PTA TSR Collector

A Python-based collection and analysis pipeline for extracting **Temporary Speed Restriction (TSR)** data from Public Transport Authority of Western Australia (PTA) Weekly Notices.

The project discovers PTA Weekly Notice PDFs, downloads them, extracts the **Current Temporary Speed Restrictions / Current Speed Restrictions** table from each notice, stores the results in a resumable SQLite database, assigns recurring restrictions to master TSR records, and exports analysis-ready CSV files.

> [!TIP]
>
> **Eventual project goal**
> A Power BI view that includes a map overlay of Perth's rail network, to see repeat hotspots for speed restrictions and maintenance issues.

> [!WARNING]
>
> **Project still under active development; data set and analysis not yet complete, comprehensive and validated**
> Version 2.4.2 introduces a repair-first extraction pipeline, parser artefact filtering, multi-strategy extraction, manual review support, and hard quality gates before analytics export.
>
> The collector can be treated as an authoritative pipeline for the PTA TSR dataset, but any new extraction run should still be reviewed with the generated data-quality and rejection diagnostics before relying on it for conclusions.
>
> Any new full run should be validated with the post-full-run checklist before Power BI is refreshed.

## Table of contents

- [Table of contents](#table-of-contents)
- [Why this project exists](#why-this-project-exists)
- [What the collector does](#what-the-collector-does)
- [What changed in v2.4.2](#what-changed-in-v242)
- [What is unique about this project](#what-is-unique-about-this-project)
  - [PTA document browser support](#pta-document-browser-support)
  - [Mixed historical folder structures](#mixed-historical-folder-structures)
  - [Mixed historical filename formats](#mixed-historical-filename-formats)
  - [Mixed historical table layouts](#mixed-historical-table-layouts)
  - [Location and distance splitting](#location-and-distance-splitting)
  - [Repair-first extraction](#repair-first-extraction)
  - [Parser artefact handling](#parser-artefact-handling)
  - [Line and direction inference](#line-and-direction-inference)
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
  - [Create diagnostics folder](#create-diagnostics-folder)
- [Output files](#output-files)
  - [`pta_tsr_occurrences.csv`](#pta_tsr_occurrencescsv)
  - [`pta_tsr_masters.csv`](#pta_tsr_masterscsv)
  - [`pta_tsr_source_pdfs.csv`](#pta_tsr_source_pdfscsv)
- [Manual review workflow](#manual-review-workflow)
  - [When manual review is needed](#when-manual-review-is-needed)
  - [Files generated for manual review](#files-generated-for-manual-review)
  - [How to edit the manual review CSV](#how-to-edit-the-manual-review-csv)
  - [How to reprocess reviewed rows](#how-to-reprocess-reviewed-rows)
  - [What not to manually review](#what-not-to-manually-review)
- [Quality gates and data assurance](#quality-gates-and-data-assurance)
- [Post-full-run validation checklist](#post-full-run-validation-checklist)
  - [1. Confirm overall collector status](#1-confirm-overall-collector-status)
  - [2. Check rejection categories](#2-check-rejection-categories)
  - [3. Check recent processed PDF extraction counts](#3-check-recent-processed-pdf-extraction-counts)
  - [4. Check parser artefacts separately](#4-check-parser-artefacts-separately)
  - [5. Generate compact rejection diagnostics](#5-generate-compact-rejection-diagnostics)
  - [6. Inspect key analytics outputs before Power BI refresh](#6-inspect-key-analytics-outputs-before-power-bi-refresh)
  - [7. Decide whether manual review is needed](#7-decide-whether-manual-review-is-needed)
  - [8. Only refresh Power BI after validation passes](#8-only-refresh-power-bi-after-validation-passes)
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

## What changed in v2.4.2

Version 2.4.2 is a significant reliability and workflow update.

Major changes:

- **Repair-first extraction**: rows are repaired and normalised before being rejected.
- **Parser artefact filtering**: blank rows and low-content fragments are counted separately instead of being treated as rejected TSRs.
- **Multiple extraction strategies**: table-line, table-text and text-line fallback extraction are scored and selected per page.
- **Speed text normalisation**: variants such as `80km’h`, `80kmh`, `80kph`, `80 km/h` and `80 km / h` are normalised to `80km/h`.
- **Chainage parsing improvements**: decimal ranges such as `2.465 to 2.780` are accepted even when the source omits `km` after each number.
- **Named-location acceptance**: historical rows such as `Fremantle - Robbs Jetty Section` are accepted when speed and reason are otherwise valid.
- **Line and direction inference**: common Perth network locations are mapped to inferred line names, line keys and directions where possible.
- **Manual review workflow**: reviewable unresolved rows are exported to a user-editable CSV with an associated `.txt` instruction file.
- **Review reprocessing**: reviewed rows can be applied with `apply-review` and then included in exported analytics.
- **Hard quality gates**: analytics export is blocked when the current dataset is clearly unsafe to use, such as when the latest processed notice has zero accepted rows.

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

### Repair-first extraction

Earlier versions were too quick to quarantine rows. Version 2.4.2 attempts to recover valid TSR rows before rejection.

The extraction pipeline is:

```text
PDF page
  -> identify likely TSR section
  -> attempt multiple extraction strategies
  -> score extraction strategies
  -> skip blank parser artefacts
  -> normalise OCR/text issues
  -> recover speed, location, date, reason and cancellation fields
  -> infer line, direction, chainage and reason group
  -> accept valid rows
  -> quarantine only meaningful unresolved rows
```

### Parser artefact handling

PDF table extraction can detect row boundaries without successfully extracting text from cells. That can create rows such as:

```json
["", "", "", "", "", ""]
```

Version 2.4.2 treats these as parser artefacts. Parser artefacts are not accepted rows, not rejected TSR rows, and not manual review rows. Parser artefact counts are tracked separately through `artifact_row_count`, `tsr_artifact_row`, `pta_tsr_source_pdfs.csv`, and `pta_tsr_data_quality_summary.csv`.

### Line and direction inference

The collector attempts to infer useful line and direction fields from line codes, corridor names and location text.

Examples:

```text
Leederville - Stirling ... Down Main -> Joondalup Line / Down
Joondalup - Edgewater ... Down Main  -> Joondalup Line / Down
Fremantle - Shenton Park ... Up Main -> Fremantle Line / Up
Glen Iris to Cockburn ... Up & Down  -> Mandurah Line / Bidirectional
Kwinana to Wellard ... Down Main     -> Mandurah Line / Down
Nowergup Yard                        -> Joondalup Line / Yard
```

Where inference is not possible, the collector uses review-visible values such as `UNCLASSIFIED`, `Unclassified`, or `Unknown` rather than leaving key analytical fields blank.

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

### Create diagnostics folder

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


## Manual review workflow

### When manual review is needed

Manual review is needed when the collector finds a row with enough content to possibly be a TSR, but not enough certainty to accept automatically.

Examples include:

- missing, contradictory or shifted fields;
- a reason field that looks like a shifted cancellation token;
- a row that contains speed/date information but lacks enough location or reason context;
- an extraction result that may be valid but needs user confirmation.

Manual review is not intended for blank parser artefacts.

### Files generated for manual review

Run:

```powershell
py .\pta_tsr_collector.py export-review-template
```

The script creates:

```text
pta_tsr_data\review\manual_rejection_review_template.csv
pta_tsr_data\review\manual_rejection_review_template.txt
```

The `.txt` instruction file explains how to edit the CSV and how to reprocess reviewed rows after user action.

### How to edit the manual review CSV

For each row:

- Read `cell_preview` and `raw_row_json`.
- If the row is a valid TSR, set `accept_row` to `1` and fill in the corrected fields.
- If the row is not a valid TSR and should be ignored in future, set `accept_row` to `0`.
- If unsure, leave `accept_row` blank and optionally add `review_notes`.

Required fields when `accept_row=1`:

```text
corrected_location
corrected_max_speed
corrected_reason
```

Recommended fields when available:

```text
corrected_distance_km
corrected_stn_no
corrected_date_imposed
corrected_date_cancelled
corrected_line_key
corrected_line_name
corrected_location_direction
```

### How to reprocess reviewed rows

After editing and saving the CSV, run:

```powershell
py .\pta_tsr_collector.py apply-review --review-file .\pta_tsr_data\review\manual_rejection_review_template.csv
py .\pta_tsr_collector.py export
```

Rows with `accept_row=1` are inserted into `tsr_occurrence` and included in exported CSVs. Rows with `accept_row=0` are marked as `ignored` and excluded from future review templates. Rows with blank `accept_row` remain unresolved.

### What not to manually review

Do not manually review parser artefacts such as:

```json
["", "", "", "", "", ""]
```

Do not manually review standalone fragments unless there is enough surrounding context to reconstruct a complete TSR row, such as:

```text
Direction
Up Main
Down Main
40.874km to
40.747km Up
```

Version 2.4.2 is designed to exclude these from the manual review template and count them separately as parser artefacts.

## Quality gates and data assurance

Version 2.4.2 refuses to export analytics when the data appears unsafe.

Blocking conditions include:

- no accepted TSR rows exist;
- the latest processed notice has zero accepted TSR rows;
- accepted rows are missing required normalised fields such as line, direction, affected area or reason group.

If export is refused, run:

```powershell
py .\pta_tsr_collector.py status
py .\pta_tsr_collector.py rejection-diagnostics
```

Then review:

```text
pta_tsr_data\logs\pta_tsr_collector.log
pta_tsr_data\exports\pta_tsr_source_pdfs.csv
pta_tsr_data\diagnostics\rejection_review_compact_YYYYMMDD_HHMMSS\01_rejection_summary.csv
pta_tsr_data\diagnostics\rejection_review_compact_YYYYMMDD_HHMMSS\02_rejection_samples.csv
```

A high parser artefact count is not automatically a data error, but it is a signal that a layout-specific extractor path may need further tuning.

## Post-full-run validation checklist

Run this checklist after a full historical backfill or after a major extractor update before refreshing Power BI or committing generated analytics files.

### 1. Confirm overall collector status

```powershell
py .\pta_tsr_collector.py status
```

Review these values:

```text
processed
failed
missing
TSR masters
TSR occurrences
Manual review rows
Parser artefacts skipped
Latest processed notice
Latest accepted TSR rows
```

Interpretation:

- `TSR occurrences` should increase as PDFs are processed. If processed PDFs increase but accepted occurrences stop increasing, the extractor may be failing a layout.
- `Latest accepted TSR rows` should normally be greater than zero when the latest Weekly Notice contains current TSRs.
- `Manual review rows` should be plausible and should not be dominated by blank or near-blank extraction debris.
- `Parser artefacts skipped` can be non-zero. Artefacts are extraction debris, not rejected TSRs, but a sudden spike in recent notices should be investigated.

### 2. Check rejection categories

Use this command to confirm that rejected rows are genuine manual-review rows rather than parser artefacts:

```powershell
py -c "import sqlite3; c=sqlite3.connect('pta_tsr_data/pta_tsr.sqlite3'); c.row_factory=sqlite3.Row; [print(f'{r[0]} | {r[1]}: {r[2]}') for r in c.execute('select rejection_category, reject_reason, count(*) from tsr_rejected_row group by rejection_category, reject_reason order by count(*) desc')]"
```

Expected pattern:

```text
manual_review_required | missing_or_invalid_speed: <count>
manual_review_required | missing_location_or_speed: <count>
manual_review_required | invalid_reason_or_shifted_columns: <count>
```

Interpretation of rejection categories:

- `manual_review_required` means the row has enough content to potentially be a TSR but was not safe enough to accept automatically.
- `parser_artifact` should generally not appear in `tsr_rejected_row`. Parser artefacts should be counted separately via `artifact_row_count` and `tsr_artifact_row`.

Interpretation of common rejection reasons:

- `missing_or_invalid_speed` means the row did not contain a speed that could be confidently normalised, or the speed appeared in an unresolved layout pattern.
- `missing_location_or_speed` means a required location or speed field was still missing after repair attempts.
- `invalid_reason_or_shifted_columns` means the reason field looked like a shifted value, such as a cancellation token, direction fragment, date fragment, STN value, or other non-reason text.

### 3. Check recent processed PDF extraction counts

Use a parameterised SQLite query to avoid PowerShell/Python/SQL quoting problems:

```powershell
py -c "import sqlite3; c=sqlite3.connect('pta_tsr_data/pta_tsr.sqlite3'); c.row_factory=sqlite3.Row; [print(f'{r[0]}: accepted={r[1]}, rejected={r[2]}, artefacts={r[3]}') for r in c.execute('select notice_date, tsr_row_count, rejected_row_count, artifact_row_count from source_pdf where status=? order by notice_date desc limit 20', ('processed',))]"
```

Do not use this broken form:

```powershell
where status=''processed''
```

Inside a Python single-quoted string, `''processed''` is interpreted as adjacent Python string literals and becomes `status=processed`, which SQLite treats as a column name rather than the text value `'processed'`.

Interpretation:

- Recent notices should usually have non-zero `accepted` counts if the source PDF contains current TSRs.
- `rejected` should be reviewed if it spikes on a recent notice.
- `artefacts` should normally be low for recent notices. High artefact counts can indicate that the extractor selected a poor page/table strategy.

### 4. Check parser artefacts separately

```powershell
py -c "import sqlite3; c=sqlite3.connect('pta_tsr_data/pta_tsr.sqlite3'); c.row_factory=sqlite3.Row; [print(f'{r[0]}: {r[1]}') for r in c.execute('select notice_date, artifact_row_count from source_pdf where artifact_row_count > 0 order by notice_date desc limit 20')]"
```

Interpretation:

- Historical artefacts are expected in some older layouts.
- Recent/current artefact spikes should be investigated before refreshing Power BI.
- Artefacts are not manual review rows unless the extractor preserved enough content to classify the row as `manual_review_required`.

### 5. Generate compact rejection diagnostics

```powershell
py .\pta_tsr_collector.py rejection-diagnostics
```

This creates a timestamped folder under:

```text
pta_tsr_data\diagnostics\rejection_review_compact_YYYYMMDD_HHMMSS\
```

Review these files:

```text
01_rejection_summary.csv
02_rejection_samples.csv
03_manual_review_template.csv
03_manual_review_template.txt
README.txt
```

Use the three CSV files for Copilot review if further extractor tuning is needed. The `.txt` file explains local user action for the review template.

### 6. Inspect key analytics outputs before Power BI refresh

After a successful export, inspect:

```text
pta_tsr_data\analytics\pta_tsr_active_current.csv
pta_tsr_data\analytics\pta_tsr_active_by_line.csv
pta_tsr_data\analytics\pta_tsr_active_by_cause.csv
pta_tsr_data\analytics\pta_tsr_data_quality_summary.csv
```

Minimum checks:

- `pta_tsr_active_current.csv` should have a plausible number of rows for the latest processed Weekly Notice.
- `line_key`, `line_name`, `location_direction`, `affected_area` and `reason_group` should be populated.
- `pta_tsr_active_by_line.csv` should not collapse into a single blank line row.
- `pta_tsr_active_by_cause.csv` should not collapse into a single blank reason row.
- `pta_tsr_data_quality_summary.csv` should show accepted rows, manual-review rows and parser artefacts separately.

### 7. Decide whether manual review is needed

If `manual_review_required` rows remain and the rows matter for analysis, export or use the generated manual review template:

```powershell
py .\pta_tsr_collector.py export-review-template
```

Edit:

```text
pta_tsr_data\review\manual_rejection_review_template.csv
```

Read the associated instruction file before editing:

```text
pta_tsr_data\review\manual_rejection_review_template.txt
```

After editing the CSV, apply corrections and regenerate exports:

```powershell
py .\pta_tsr_collector.py apply-review --review-file .\pta_tsr_data\review\manual_rejection_review_template.csv
py .\pta_tsr_collector.py export
```

### 8. Only refresh Power BI after validation passes

Refresh Power BI only after:

- `export` succeeds without a quality-gate error;
- latest accepted TSR rows are plausible;
- active-current rows are populated with line and reason fields;
- rejection and artefact counts have been reviewed;
- manual corrections, if any, have been applied and exports regenerated.

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
