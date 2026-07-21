# corpus/ — messy real-world files for taxonomy validation (stream A)

Test data for validating `docs/taxonomy.md` against reality: run the profiler across
every file here, audit what it finds and what it misses. Companion evidence corpus
(streams B/D — documents, not data) lives at `~/repos/etl-evidence`.

- `manifest.json` — every entry: source, license, why included. The corpus IS this file.
- `fetch_corpus.py` — reproduces `raw/` from the manifest (`python3 corpus/fetch_corpus.py`).
- `manifest.lock.json` — observed sha256 per fetched file (drift detection).
- `raw/` — the files (gitignored; never committed).

Composition targets: parser test fixtures (curated failure cases from other tools' bug
history), genuine portal exports (US/UK/EU/JP — locale, encoding, preamble/footer
diversity), teaching messy datasets. Selection-bias caveat: everything here is public;
proprietary vendor feeds are unrepresented and findings should say so.
