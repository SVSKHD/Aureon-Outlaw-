# Aureon Outlaw

The XAUUSD MT5 price-action + session-scout bot lives in [`scout/`](scout/) — that directory is the
single source of truth for the package, its tests, its configuration and its documentation.

```
scout/
  src/xau_mt5_bot/     the package (engine, scouts, decision router, cards, MT5 adapter)
  tests/               pytest suite — run with `pytest -q` from scout/
  config.yaml          the one configuration file
  run.bat              trading loop (via supervisor.py)
  run_discord_bot.bat  read-only Discord command bot
  README.md            full documentation
  RELEASE_NOTES_*.md   one file per version (latest: v3.3.0)
```

Start with [`scout/README.md`](scout/README.md) and
[`scout/WINDOWS_DEPLOYMENT.md`](scout/WINDOWS_DEPLOYMENT.md).

The repository root previously carried a byte-identical copy of every document and launcher script
in `scout/` (but no package or tests, so it could not run). Those duplicates were removed in v3.3.0.
