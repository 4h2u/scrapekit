# scrapekit

Registry-driven extraction client. You describe each site once in YAML — URL,
wait selector, minimum rows, and one or more extractor scripts — and the engine
runs them through a browser worker, scores the results, and writes CSV or XLSX.

This repository is the **client half**. It speaks one JSON request per line to
a browser worker on a unix socket (`SCRAPEKIT_SOCK`, default
`/tmp/scrapekit.sock`). The worker (a Playwright/Camoufox daemon that owns the
browser, pacing and challenge classification) is not part of this repo.

## What it actually does

- **Fallback extractors.** Each site lists extractor scripts. The engine runs
  them in order, keeps the best by *complete rows* first and field fill second,
  and stops after at most two extra evaluations. Selector drift degrades the
  result instead of zeroing it.
- **Canary.** `--canary` exits 1 when a site is under `min_rows` with no block
  markers (selector drift), or when the worker socket is unreachable. A block,
  rate limit or unsolved challenge is `WARN` and exits 0: a blocked host should
  not page anyone at 7am, while silent selector drift should.
- **Normalisation and dedupe.** Whitespace collapse, price parsing across
  `1.234,56` / `1,234.56` / `51.77` / `140,08`, relative dates to `15h` / `3d`,
  dedupe by a per-site key, CSV or XLSX export.

## Quickstart

```bash
pip install -e .                               # installs pyyaml + openpyxl
export SCRAPEKIT_SOCK=/tmp/scrapekit.sock      # your worker socket

scrapekit --list   --registry sites.example.yaml
scrapekit --all    --registry sites.example.yaml --out out.csv
scrapekit --canary --registry sites.example.yaml
```

Run from a checkout without installing: `PYTHONPATH=src python3 -m scrapekit ...`
with the same flags.

`--list` does not open the socket. A run and `--canary` do; without a worker
they report `unreachable` and exit 1. Add `--report out.json` to keep the run
summary.

## Registry format (`sites.example.yaml`)

| Field | Meaning |
| --- | --- |
| `name` | unique site id |
| `url` | page to open |
| `wait` | CSS selector that must exist after load |
| `min_rows` | canary threshold |
| `key` | field used for dedupe |
| `js` | ordered list of extractor scripts (first with complete rows wins) |
| `page_url` | optional template for pagination, e.g. `"https://site/page/{page}/"`; without it the engine appends `?page=N` |
| optional | `pages` / `paginate` / `max_pages`, `scroll` / `scroll_until`, `ready_js`, `delay`, `ui_search` (`until_url`), `human` |

The example registry uses four public scraping sandboxes
(books.toscrape.com, quotes.toscrape.com, news.ycombinator.com, the
webscraper.io test site). No login sites, no customer data.

## Limitations

- No browser included. The worker is external and private.
- Fixed waits and per-host pacing only. No proxy rotation, no fingerprint
  changes, no captcha solving. An interactive captcha needs a human; the
  `human` option hands the page to one and waits.
- The normaliser is tuned for prices and counts, not locale-specific formats.
- `examples/sample_output.csv` is synthetic and clearly marked; the datasets in
  the portfolio sample pack were produced with this engine plus the private
  worker.

## License

MIT. Copyright (c) 2026 4h2u. See [LICENSE](LICENSE).
