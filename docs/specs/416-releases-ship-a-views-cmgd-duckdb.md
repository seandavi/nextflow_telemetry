# Spec #416: releases ship a views `cmgd.duckdb`; stop documenting `catalog.ducklake`

Issue: [#416](https://github.com/seandavi/nextflow_telemetry/issues/416). There is no GitHub
parent issue (the sub-issues API returns 404). The related issue is
[#417](https://github.com/seandavi/nextflow_telemetry/issues/417) (partition large tables;
content-addressed files). This spec builds the views from `files.json`, so #417 can add
files per table without changing the views code. The #381 migration (#370–#380, ADR-0013)
is about the control plane and does not touch `etl/`.

## Goal

`nf-etl publish` writes `<release>/cmgd.duckdb`, a DuckDB database that holds only views.
There is one view per release table, named after the table. Each view is
`SELECT * FROM read_parquet([...])` over the absolute public URLs of the files listed in
that table's `tables/<table>/files.json`. A reader runs one statement and queries
`cmgd.<table>`:

```sql
ATTACH 'https://cmgd-public.cancerdatasci.org/<dataset>/<release>/cmgd.duckdb' AS cmgd;
```

This becomes the documented entry point in `docs/data-access.md` (SQL, Python and R), the
release `README.md`, the generated `index.html` query examples and `llms.txt`.
`catalog.ducklake` is still built (by cdsci-lake's `publish_release`) and uploaded, but no
page or doc mentions it any more.

Measured in the issue on `cmgd_mpa4.2-2.3.0/2026-10-20`: 274 KB instead of 1.5 MB,
0.9–1.2 s per query instead of 2.2–2.4 s, and no `ducklake` extension needed.

## Non-goals

- No change to the data files, their layout, `manifest.json`, `files.json`,
  `schema.json`, `provenance.json`, `studies/` or `genefamilies/`.
- No removal of `catalog.ducklake`. It comes from cdsci-lake's `publish_release` (the
  contract's default `required_artifacts` includes `ducklake`). Dropping it is a cdsci-lake
  change, tracked separately. `test_frozen_catalog_attaches_over_http` stays.
- `cmgd.duckdb` is not listed in `manifest.json` and has no checksum in any index. cdsci-lake
  has no hook for extra artifacts (cdsci-lake#134, ADR-0011). It is a derived file of
  views, not data.
- No backfill of releases that are already live. They get `cmgd.duckdb` when their
  registration is next published (see the rollout under Risks).
- No upload in tests and no network in tests.
- No change to `sync_commands`. `rclone copy --immutable` already uploads every file in the
  release directory, so `cmgd.duckdb` goes with the tables, before the pointer files.
- No change to the other snippets on the release page (direct `read_parquet` of
  `data/part-00000.parquet`). They keep showing how to read Parquet without attaching
  anything. If #417 lays out multi-file tables, rewriting them is #417's job.
- No Alembic migration, no Postgres change, no API endpoint and no new env var.
- No change to `nf-client`, the frontend, `health_report.py`, `cf/`, `contract/`, the
  control plane or `deliver`.

### Departures from the design record

- **ADR-0011** (Consequences: "Readers attach a frozen catalog over HTTPS with DuckDB").
  After this change readers attach `cmgd.duckdb`, and the frozen catalog is built but not
  documented. The spec adds a short amendment to ADR-0011 (Design §6). The rest of
  ADR-0011 is unchanged: releases are still `publish_release` builds, uploaded by `--sync`.
- ADR-0013, 0007, 0010 and 0012 are not affected. The views read only the release's own
  Parquet, and source metadata values reach a release only through the existing metadata
  tables.

## Design

### Key constraint, verified: `CREATE VIEW` binds at creation

With DuckDB 1.5.6 (the locked version), `CREATE VIEW v AS SELECT * FROM
read_parquet(['https://…'])` binds the query when the view is created, which issues an
HTTP HEAD for the first URL. It fails if the file isn't there:
`IO Error: Could not resolve hostname error for HTTP HEAD to …`. `duckdb_settings()`
has no option that skips the bind. Relative paths don't help either: a view over
`read_parquet(['tables/t/…'])` inside a database attached over HTTP resolves against
the client's working directory, not the database's URL (`No files found that match the
pattern`, also verified).

`nf-etl publish` builds the release locally, and `--sync` uploads it later. So when the
views are created, their public URLs don't exist yet. omicidx builds its views file after
its Parquet is public, which is why it doesn't hit this.

**Solution:** during the build, register an fsspec filesystem on the build connection that
serves the release's public URLs from the local release directory
(`DuckDBPyConnection.register_filesystem(filesystem: fsspec.AbstractFileSystem)`, in
duckdb's Python API). The view binds against the local bytes, but its stored SQL keeps the
public URL. Verified with duckdb 1.5.6 and fsspec 2026.9.0:

- The registered filesystem takes precedence over `httpfs` for `https://` and `http://`,
  even when `httpfs` is already loaded.
- The stored SQL is `CREATE VIEW t AS SELECT * FROM read_parquet(main.list_value('https://…/part-00000.parquet'));`.
- A fresh client running `ATTACH '<http url>/cmgd.duckdb' AS cm` gets a read-only
  database (`duckdb_databases().readonly = true`) and the view's row count is correct.
- Closing the build connection leaves no `cmgd.duckdb.wal`.
- The file is tagged `storage_version: v1.0.0+` (the default
  `storage_compatibility_version` is `v0.10.2`), so any DuckDB 1.0 or newer can attach
  it. No storage setting is needed.

### 1. `pyproject.toml`, `uv.lock`

Add `fsspec` as a direct dependency. It is already installed (2026.9.0, pulled in by
`huggingface-hub`), so the lock doesn't change versions. `etl/publish.py` now imports it
directly, so it must be declared rather than left transitive:

```toml
    "fsspec>=2026.9.0",           # nf-etl publish: serve release URLs locally while cmgd.duckdb's views bind
```

Run `uv lock` so the project's dependency list in `uv.lock` records it. No other lock
changes are expected.

### 2. `src/nextflow_telemetry/etl/publish.py`

Imports to add: `from fsspec.implementations.dirfs import DirFileSystem  # type:
ignore[import-untyped]` and `from fsspec.implementations.local import LocalFileSystem  #
type: ignore[import-untyped]` (fsspec has no `py.typed`; with these ignores and the
annotations below, mypy passes, verified).

Constant:

```python
# The release's DuckDB entry point (#416): one view per table over its public Parquet URLs.
VIEWS_DB = "cmgd.duckdb"
```

Mirror (module-private):

```python
class _ReleaseMirror(DirFileSystem):
    """Serves ``<release_url>/<path>`` from ``<release_dir>/<path>``, so cmgd.duckdb's
    views bind before the release is uploaded (CREATE VIEW reads the first file)."""
    protocol = ("https", "http")

    def __init__(self, release_url: str, release_dir: Path) -> None:
        super().__init__(path=str(release_dir), fs=LocalFileSystem())
        self.release_url = release_url

    def _join(self, path: str | list[str]) -> str | list[str]:
        if isinstance(path, str):
            path = path.removeprefix(self.release_url + "/")
        return super()._join(path)
```

`_join` is the hook that `DirFileSystem` routes every path through. A URL outside the
release maps to a path that doesn't exist, so `CREATE VIEW` fails loudly. It never reads
anything outside `release_dir`.

Writer:

```python
def write_views_db(out: Path, release_dir: Path, release_url: str) -> None:
    """``out`` (cmgd.duckdb): one view per table in ``release_dir``'s manifest.json,
    ``read_parquet`` over the absolute URLs of the files in the table's files.json
    (``<release_url>/<dir of the table's "files" path>/<uri>``), in files.json order. Views only:
    a reader ATTACHes it and DuckDB fetches the Parquet a query needs."""
```

Behaviour:

- Read `release_dir / "manifest.json"`. For each entry in `manifest["tables"]`, in
  manifest order, read `release_dir / table["files"]` (the JSON key is `files`; cdsci-lake's
  `ManifestTable` names the dataclass field `files_path`). The URL of each
  `files[*].uri` is
  `f"{release_url}/{posixpath.dirname(table['files'])}/{f['uri']}"`, which is
  `…/tables/<table>/data/part-00000.parquet` today.
- `con = duckdb.connect(str(out))`, then
  `con.register_filesystem(_ReleaseMirror(release_url, release_dir))`, then for each table
  `CREATE VIEW "<name>" AS SELECT * FROM read_parquet([<'url'>, …])`. Escape `'` as `''`
  inside URL literals. Use plain `CREATE VIEW`, not `OR REPLACE`: `out` is always new.
- Close the connection in a `finally` block. No other objects go in the database: no
  tables, no schemas, no metadata row.

Hook it into `publish()`, inside the `with tempfile.TemporaryDirectory(...)` block, after
`release_dir = root / contract.id / manifest.release` and before the
`for extra in sorted(extras.iterdir())` loop that moves extras into the release:

```python
        release_url = f"{public_base_url.rstrip('/')}/{contract.id}/{manifest.release}"
        write_views_db(extras / VIEWS_DB, release_dir, release_url)
```

Because it is built in the staging directory, a failed build leaves no partial file in
the release. The existing loop moves it into `release_dir`. Then, after `add_provenance`
and before `site.write_study_pages`, call `add_readme_query(release_dir, release_url)`
(`release_url` is still in scope).

README section (the release `README.md` is cdsci-lake's `render_dataset_markdown`, written
by `build_release`, outside `files.json` and the manifest, so `verify_release` doesn't
check it, the same as `provenance.json` in ADR-0011's amendment §10):

```python
def add_readme_query(release_dir: Path, release_url: str) -> None:
    """Append a "Query this release" section to the release README.md."""
```

It appends exactly this text (an f-string with `release_url`, and a real table name):

````markdown

## Query this release

With [DuckDB](https://duckdb.org) 1.5.2 or newer, attach the release's views. DuckDB
fetches only the Parquet a query needs:

```sql
INSTALL httpfs; LOAD httpfs;
ATTACH '<release_url>/cmgd.duckdb' AS cmgd;
SELECT study_name, count(*) AS n_samples FROM cmgd.samples GROUP BY 1 ORDER BY 1;
```
````

Module docstring: in the layout block, replace `catalog.ducklake` with `cmgd.duckdb`, and in
the prose say that publish writes `cmgd.duckdb` and appends to the README after
`publish_release` returns. Also keep saying that `publish_release` still writes a frozen
DuckLake catalog. No other code in `publish.py` changes.

### 3. `src/nextflow_telemetry/etl/site.py`

- `_snippet_list`: the first snippet becomes:

  ```python
  out = [f"""-- DuckDB 1.5.2+: attach the release's views (read-only, fetches only what a query needs)
  INSTALL httpfs; LOAD httpfs;
  ATTACH '{u}/cmgd.duckdb' AS cmgd;
  SELECT study_name, count(*) AS n_samples FROM cmgd.samples GROUP BY 1 ORDER BY 1;""",
  ```

  The other three or four snippets don't change.
- `_files_table`: the extra-file list becomes `("manifest.json", "provenance.json",
  "cmgd.duckdb", "README.md", "studies/index.json")`. Delete the
  `art = … "ducklake" …` lookup. Every extra row's sha256 cell is empty, as it already is
  for `README.md`. Rows are skipped when the file doesn't exist (existing behaviour), so
  older releases without `cmgd.duckdb` render as before, minus the catalog row.
- `llms_txt`: replace the line
  `f"- DuckLake catalog: {base}/{{dataset}}/{{release}}/catalog.ducklake"` with
  `f"- DuckDB views over every table (ATTACH it): {base}/{{dataset}}/{{release}}/cmgd.duckdb"`.

After this, `site.py` contains no `catalog.ducklake` or `ducklake` string.

### 4. `src/nextflow_telemetry/etl/mirror.py` (text only)

- `release_files` docstring: "tables, catalog, manifest" becomes "tables, cmgd.duckdb,
  manifest".
- `zenodo_metadata` description: "Parquet tables, a read-only DuckLake catalog, manifest
  with per-file sha256" becomes "Parquet tables, cmgd.duckdb (DuckDB views over the
  canonical copy's Parquet URLs), manifest with per-file sha256".
- `CARD`: "frozen\nDuckLake catalog" becomes "cmgd.duckdb views".

The upload set doesn't change: `catalog.ducklake` and `cmgd.duckdb` are both files in the
release directory. A mirror's `cmgd.duckdb` reads from cmgd-public, which the new wording
says.

### 5. Docs

- `docs/data-access.md`:
  - Layout block: replace the `catalog.ducklake` line with
    `cmgd.duckdb                     DuckDB database: one view per table over the Parquet below`.
  - "SQL with DuckDB": the intro becomes "Attaching the release's `cmgd.duckdb` (a few
    hundred KB of views) is instant: DuckDB fetches only the parts of the Parquet files a
    query touches." The block becomes:

    ```sql
    INSTALL httpfs; LOAD httpfs;
    ATTACH 'https://cmgd-public.cancerdatasci.org/cmgd_nextflow-2.2.1/2026-10-08/cmgd.duckdb' AS cmgd;
    SELECT view_name FROM duckdb_views() WHERE database_name = 'cmgd' ORDER BY view_name;
    ```

    Replace the `DATA_PATH` paragraph with: "Each table is a read-only view over the Parquet
    files listed in its `files.json`, by absolute URL, so the database works from anywhere,
    including mirrors." Rename "Without the catalog" to "Without attaching".
  - Python: `con.execute("INSTALL httpfs; LOAD httpfs;")` and
    `con.execute(f"ATTACH '{url}/cmgd.duckdb' AS cmgd")`.
  - R: the setup loop becomes `for (sql in c("INSTALL httpfs", "LOAD httpfs"))`, and the
    attach becomes `dbExecute(con, sprintf("ATTACH '%s/cmgd.duckdb' AS cmgd", url))`.
  - After the change, the doc contains no `ducklake` string. The queries against
    `cmgd.<table>` don't change.
- `docs/etl-runbook.md` (Publishing): "sorted Parquet, a frozen read-only
  `catalog.ducklake`, …" becomes "sorted Parquet, a frozen `catalog.ducklake` (still built,
  no longer documented for readers), …". Add: "`publish` then writes `cmgd.duckdb` (one view
  per table over the release's public Parquet URLs, from `files.json`) and appends a query
  section to `README.md`."
- `README.md` line 220 (Table 2): "a read-only DuckLake catalog" becomes "a DuckDB views
  database (`cmgd.duckdb`)".
- `docs/storage-layout.md` line 42: in the brace list, replace `catalog.ducklake` with
  `cmgd.duckdb`.

### 6. `docs/adr/0011-results-storage-and-publication.md`

Append, before `## References`:

```markdown
## Amendment (2026-11-01): `cmgd.duckdb` is the reader entry point (#416)

12. **Each release has `cmgd.duckdb`**: a DuckDB database of views, one per table,
    `read_parquet` over the absolute public URLs of the table's `files.json`. Readers
    `ATTACH` it; no `ducklake` extension. Measured on a live release: 274 KB vs 1.5 MB,
    about 1 s vs 2.3 s per query. `nf-etl publish` builds it locally before upload by
    serving the release URLs from the local directory while the views bind. It is not in
    the manifest (cdsci-lake#134). `catalog.ducklake` is still built by `publish_release`
    but no longer documented; removing it is a cdsci-lake change. This replaces the first
    Consequence ("Readers attach a frozen catalog").
```

### Callers affected

- `nf-etl publish` (CLI): no flag change. `cli.py` calls `publish.publish(...)` without
  `public_base_url`, so the views get `ETL_PUBLIC_BASE_URL` (default
  `https://cmgd-public.cancerdatasci.org`), the same base the site snippets use.
- `nf-etl mirror`: uploads the extra file. Text changes only (§4).
- `scripts/smoke_public_data.py`: no code change. It runs the new doc snippets.
- `nf-client`, the frontend, `health_report.py`, the API, `deliver`, `nf-meta`: none.

### Tests

`tests/cmgd_release_fixture.py`: the `public_site` fixture passes
`public_base_url=f"{host}/public"` to `publish.publish`. The fixture's views then point at
the local server, the same way the doc test localizes the doc. This is required for any
over-HTTP query of `cmgd.duckdb`. No existing test asserts the production base in pages
(checked: `grep cancerdatasci tests/` hits only unrelated files).

`tests/test_etl_publish.py`, new tests:

1. `test_views_db_attaches_over_http_and_matches_manifest(public_site)`: for each `reg` in
   `REGS`:
   - `rdir/"cmgd.duckdb"` exists, and `rdir/"cmgd.duckdb.wal"` does not.
   - A fresh `duckdb.connect()` runs `INSTALL httpfs; LOAD httpfs;` and
     `ATTACH '{public_site.base}/{m.dataset}/{m.release}/cmgd.duckdb' AS cmgd`.
   - `{view_name for non-internal duckdb_views() in database 'cmgd'} == {t.name for t in m.tables}`,
     and `duckdb_tables()` has no rows for `cmgd`.
   - For every table, `SELECT count(*) FROM cmgd.<t>` equals `t.row_count` from the
     manifest. This is the issue's test.
   - Each view's `sql` contains `f"{public_site.base}/{m.dataset}/{m.release}/tables/{t}/{uri}"`
     for every `uri` in that table's `files.json`.
2. `test_views_db_builds_before_upload_from_every_file(tmp_path)`: a hand-made release dir
   with `manifest.json` (`{"tables": [{"name": "t", "files": "tables/t/files.json"}]}`;
   `write_views_db` reads only these keys) and a `files.json` listing two Parquet files,
   `data/part-00000.parquet` (3 rows) and `data/part-00001.parquet` (4 rows), written with
   `duckdb.sql("COPY (SELECT range AS x FROM range(n)) TO '…'")`.
   - `write_views_db(out, rdir, "https://cmgd-public.invalid/d/2026-10-08")` succeeds, with
     no network (the `.invalid` TLD never resolves). Open `out` read-only locally. The
     view's `sql` lists both URLs in `files.json` order.
   - Build a second database with the release URL of a local `RangeHandler` server
     (imported from `cmgd_release_fixture`) that serves `tmp_path`. Attach it over HTTP:
     `SELECT count(*) FROM <alias>.t` is 7.
3. Extend `test_samples_table_provenance_and_site`:
   - `llms.txt` contains `/{dataset}/{release}/cmgd.duckdb` and doesn't contain
     `catalog.ducklake`.
   - The release `index.html` contains `cmgd.duckdb` and doesn't contain `catalog.ducklake`.
   - `README.md` in `rdir` contains `ATTACH '{public_site.base}/{m.dataset}/{m.release}/cmgd.duckdb' AS cmgd;`.
   - `verify_release` still passes (already asserted there, after the pages).

Existing tests that now cover the change without edits:
- `test_site_snippets_run_against_the_release` runs the new first snippet (an `ATTACH` of
  `cmgd.duckdb` over HTTP) and requires rows.
- `test_frozen_catalog_attaches_over_http` still passes, because the catalog is still
  built.
- `test_doc_sql_python_bash_snippets_run` and the R test (skipped without R) run the new
  doc blocks.

`tests/test_data_access_docs.py`: extend
`test_doc_has_snippets_for_every_access_path` with
`assert "/cmgd.duckdb" in text and "ducklake" not in text`, where
`text = smoke.DOC.read_text()`.

## Acceptance criteria

- [ ] `nf-etl publish` writes `<release>/cmgd.duckdb` with exactly one view per manifest
      table and nothing else, and leaves no `.wal`.
- [ ] Each view reads every file in its table's `files.json`, by absolute URL
      (`<public_base_url>/<dataset>/<release>/tables/<t>/<uri>`), in `files.json` order.
- [ ] The build needs no network and works before upload (test 2 with the `.invalid`
      host).
- [ ] Attaching `cmgd.duckdb` over HTTP and counting each view matches `manifest.json`'s
      `row_count`, for every fixture registration (test 1).
- [ ] The release `README.md` ends with the "Query this release" section, which has the
      release's `cmgd.duckdb` URL.
- [ ] `index.html` snippets, the files table and `llms.txt` name `cmgd.duckdb`, and
      `site.py` has no `ducklake` string.
- [ ] `docs/data-access.md` has no `ducklake` string, and its SQL, Python and R access
      paths attach `cmgd.duckdb`. All its snippets pass against the local release.
- [ ] `catalog.ducklake` is still in every release (`test_frozen_catalog_attaches_over_http`
      passes).
- [ ] ADR-0011 has the 2026-11-01 amendment. The runbook, README Table 2, storage-layout
      and the mirror text are updated.
- [ ] `fsspec` is a direct dependency in `pyproject.toml`, and `uv.lock` is updated with no
      version changes.
- [ ] `uv run pytest tests/test_etl_publish.py tests/test_data_access_docs.py tests/test_etl_mirror.py`
      passes. `uv run pytest` passes as a whole.
- [ ] `uv run mypy src/nextflow_telemetry` passes.

## Test plan

```sh
uv run pytest -q tests/test_etl_publish.py tests/test_data_access_docs.py tests/test_etl_mirror.py
uv run pytest -q
uv run mypy src/nextflow_telemetry
grep -n ducklake src/nextflow_telemetry/etl/site.py docs/data-access.md   # expect no output
```

Baseline before the change: the first command gives 20 passed (2026-11-01, this worktree).
Optional manual check (not CI): after the rollout below, run
`uv run python scripts/smoke_public_data.py --no-r` against the live site.

## Risks

- **Live releases lack `cmgd.duckdb` until they are republished.** When the doc merges,
  `docs/data-access.md` on `main` points readers at `…/<latest release>/cmgd.duckdb`,
  which returns 404 for datasets whose newest release predates this change. The smoke
  script fails against the live site for the same reason. Rollout, done by the operator
  right after merge: for each dataset in the live root `index.json`, run
  `uv run nf-etl publish --registration <workflow_id>/<version>` and then `--sync`. This is
  the normal publish path, and it cuts a new dated release. Then run the smoke script.
  This spec doesn't backfill old releases.
- **`_join` is an fsspec internal.** If a future fsspec stops routing paths through
  `DirFileSystem._join`, the build would try the real network. Test 2 (the `.invalid`
  host) then fails, so the break is caught in CI, not in production.
- **DuckDB's fsspec bridge** (`register_filesystem`) is the Python client's documented
  API. If a DuckDB upgrade stopped preferring a registered `https` filesystem over
  `httpfs`, test 2 would catch that too.
- **`cmgd.duckdb` has no checksum** in any manifest or index (cdsci-lake#134). It holds
  only views, and its URLs point at checksummed files, so a corrupted copy fails to attach
  or reads the wrong URLs, which can be checked against `files.json`. This is the same
  status as the release pages.
- **Mirrors:** a Zenodo or Hugging Face copy's `cmgd.duckdb` reads cmgd-public, not the
  mirror. The new mirror text says so.
- **DuckDB-Wasm:** the issue reports that a plain `.duckdb` attach works there. This spec
  doesn't add a Wasm test.
- **#417 interplay:** views list explicit file URLs. If #417 partitions tables into many
  files per study, its own open question (does DuckDB prune an explicit HTTPS list
  without fetching every footer?) decides whether views should switch to per-study unions.
  This spec's `files.json`-driven builder is the seam for that.
