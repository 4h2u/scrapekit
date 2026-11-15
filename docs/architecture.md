# Architecture

```
  sites.example.yaml
          |
          v
  +----------------+   one JSON line   +---------------------------+
  | scrapekit      | -----------------> | browser worker (private)  |
  | engine         | <----------------- | Playwright + Camoufox     |
  | (this repo)    |   JSON response    | owns pages, pacing        |
  +----------------+                    +---------------------------+
          |
          v
   CSV / XLSX, dedupe, canary report
```

The engine never touches a browser directly. Each command is a request
(`open`, `goto`, `eval`, `wait`, `close`, ...) and the worker serialises them
on one thread. That keeps the client small and makes the worker replaceable.

Why the split:

- one browser process can serve many scripts without fighting over the profile
- pacing, cooldowns and challenge classification live in one place
- the client is safe to open-source: it holds no cookies, no sessions and no
  site-specific knowledge beyond the registry file
