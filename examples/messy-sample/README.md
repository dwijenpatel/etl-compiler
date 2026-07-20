# Messy-sample end-to-end demo

`messy_sample.csv` plants 17 failure modes in 10 lines: UTF-8 BOM, mixed CRLF/LF, NFD-decomposed unicode, an embedded control character, a non-breaking space, mojibake, `$1,234.56` and `(500)` numerics, ambiguous MDY dates, Y/N booleans, a leading-zero ZIP, an `N/A` sentinel, a whitespace-only value, a ragged row, an exact duplicate, a blank row, and a `Total` footer.

```bash
# Watch the profiler find all of them:
python3 ../../skill/etl-generator/scripts/profile.py messy_sample.csv

# Run the pipeline (shaped exactly like codegen output):
python3 smoke_pipeline.py     # exit 2 = completed with quarantined rows
cat etl_out/summary.json      # every fix counted, ragged row quarantined as STR-02
```

This doubles as the regression check for the runtime: if `smoke_pipeline.py` stops producing 6 output rows / 1 quarantined / exit 2, something broke.
