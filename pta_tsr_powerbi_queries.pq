// PTA TSR Collector - Power Query M starter queries for Power BI Desktop
// Use Home > Transform data > New Source > Blank Query > Advanced Editor.
// Create one query per block below, or use Get Data > Web with the same raw GitHub URLs.

let
    BaseUrl = "https://raw.githubusercontent.com/twcau/pta_tsr_collector/main/",
    CsvFromGitHub = (RelativePath as text) as table =>
        let
            Source = Csv.Document(
                Web.Contents(BaseUrl & RelativePath),
                [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]
            ),
            Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true])
        in
            Promoted
in
    CsvFromGitHub

// Create separate queries using the helper above:
// Occurrences = CsvFromGitHub("pta_tsr_data/exports/pta_tsr_occurrences.csv")
// Masters = CsvFromGitHub("pta_tsr_data/exports/pta_tsr_masters.csv")
// Source PDFs = CsvFromGitHub("pta_tsr_data/exports/pta_tsr_source_pdfs.csv")
// Line Month Summary = CsvFromGitHub("pta_tsr_data/analytics/pta_tsr_line_month_summary.csv")
// Reason Summary = CsvFromGitHub("pta_tsr_data/analytics/pta_tsr_reason_summary.csv")
// Segment Summary = CsvFromGitHub("pta_tsr_data/analytics/pta_tsr_segment_summary.csv")
// Active By Notice = CsvFromGitHub("pta_tsr_data/analytics/pta_tsr_active_by_notice.csv")
// Duration Buckets = CsvFromGitHub("pta_tsr_data/analytics/pta_tsr_duration_buckets.csv")
// Data Quality Summary = CsvFromGitHub("pta_tsr_data/analytics/pta_tsr_data_quality_summary.csv")
