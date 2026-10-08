# Measured data volumes

Sizes of the `lake.cmgd.*` tables, measured with `nf-etl volumes` (#234). They
feed the scale planning for 200–400k samples and the AWS Open Data enquiry
(#228). Re-run after the first real ingest and replace the table below.

## 2026-10-08: 5 samples, `cmgd_nextflow 2.2.1`

Five completed samples (the oldest five in the v2 backlog) ingested as one
batch into a local-backend lake: the same code path and sorted Parquet as the
shared lake, on scratch disk. Inlined rows were flushed to files before
measuring.

| table | registration | samples | rows/sample | bytes/row | bytes/sample | @200k samples | @400k samples |
|---|---|---|---|---|---|---|---|
| taxonomic_profile_metaphlan | cmgd_nextflow 2.2.1 | 5 | 857.2 | 28 B | 24.3 KB | 4.9 GB | 9.7 GB |
| taxonomic_profile_bracken | cmgd_nextflow 2.2.1 | 5 | 6,180.2 | 16 B | 100.6 KB | 20.1 GB | 40.2 GB |
| resistome | cmgd_nextflow 2.2.1 | 5 | 89.2 | 44 B | 3.9 KB | 786.9 MB | 1.6 GB |
| qc_metrics | cmgd_nextflow 2.2.1 | 5 | 1.0 | 1.0 KB | 1.0 KB | 200.2 MB | 400.4 MB |
| marker_abundance | cmgd_nextflow 2.2.1 | 5 | 36,869.0 | 13 B | 481.4 KB | 96.3 GB | 192.5 GB |
| marker_presence | cmgd_nextflow 2.2.1 | 5 | 34,149.6 | 10 B | 347.4 KB | 69.5 GB | 139.0 GB |
| **lake total** | cmgd_nextflow 2.2.1 | | | | 958.6 KB | 191.7 GB | 383.4 GB |

Columns: rows/sample counted in the table; bytes/row is the live Parquet file
bytes over their record counts (Snappy, the DuckLake default); bytes/sample is
rows/sample × bytes/row; the last two columns scale bytes/sample linearly.

Caveats:

- Five samples is a small sample of one registration. Real batches are 500
  samples, so files are ~100× larger and Parquet footers matter less; the
  qc_metrics figure (1 KB/row at 5 rows) is mostly footer.
- Markers are 91% of the rows and 86% of the bytes (marker_abundance +
  marker_presence: 71k of 78k rows/sample).
- **HUMAnN gene families and pathways are not measured yet**: no 2.3.0 HUMAnN
  registration has published samples to `cmgd-raw`. Once some are ingested,
  `nf-etl volumes` adds a `gene families (cmgd-raw files)` row per registration
  from `lake.cmgd.humann_genefamilies_files`: rows/sample (parsed), bytes/row
  and bytes/sample of the gzipped TSV in `cmgd-raw`, extrapolated the same way.
- Ingest throughput on onclappc02 for this run: 5 samples in 6.7 s
  (58k rows/s; 8 samples fetched in parallel, rclone-bound).
