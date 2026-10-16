# Reviewer-agent smoke test (plan §5, #271)

Can someone who has never seen this project get from the landing page to a
sample's profile and a study's metadata in **4 fetches or fewer**? A fetch is
one HTTP GET or one DuckDB query. Use a fresh agent with no repo access, only
WebFetch (or curl) and duckdb, and give it only the prompt below.

## Prompt

> Start at `<LANDING>` (the site's `index.html`; `llms.txt` sits next to it).
> Use at most 4 fetches, where one HTTP GET or one DuckDB query counts as one
> fetch. Don't guess URLs that aren't written on a page you fetched.
>
> 1. For the sample with run accession `<RUN>`, report its top 5 MetaPhlAn
>    species with their relative abundance.
> 2. For study `<STUDY>`, report its sample metadata, with the source and
>    source release of each value. If the release has no metadata, say so and
>    say where the page says that.
>
> Report your answers, the URL or query behind each fetch, and the number of
> fetches you used.

## Pass condition

- Both answers are correct, checked against the release's Parquet.
- It takes 4 fetches or fewer.
- No URL is guessed.

Run it three times with different `<RUN>` / `<STUDY>` pairs. Record the results
on #271.

## Expected path

1. GET `index.html`. It gives the release, the Parquet URLs and a top-5 query
   template, and says whether the release carries metadata.
2. DuckDB over `samples` (or `taxonomic_profile_metaphlan` filtered on
   `run_ids`) to get the profile.
3. DuckDB over `sample_metadata_harmonized` filtered on `study_name`. Its
   `source` and `release` columns give each value's source and source release.
