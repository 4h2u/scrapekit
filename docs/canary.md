# Canary: drift vs outage

A scraper fails in two very different ways, and they need different fixes:

| Outcome | Exit | Meaning | Action |
| --- | --- | --- | --- |
| `ok` | 0 | rows >= min_rows | none |
| `BLOCK` | 0 (WARN) | page is a block, rate limit or unsolved challenge | wait, rotate the egress, or hand it to a human |
| `LOW` | 1 (FAIL) | fewer rows than min_rows with no block markers | selector drift: refresh the extractor |
| `DOWN` | 1 (FAIL) | worker socket unreachable | start the worker |

A block is a `WARN`, drift and a dead worker are `FAIL`. That is deliberate:
a blocked host should not wake anyone at 7am, silent selector drift should.

Run it from cron or a systemd timer and alert on the exit code. The report
lists, per site: rows, extractor used, state, and wall time.
