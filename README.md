# Tape

Companies file hundreds of SEC 8-Ks every day. Almost all of them are noise. A handful move the stock.

Tape reads every one, asks [Jev](https://www.builder.io/blog/what-is-jev) five quick questions about it, and lets an always-on [Agent37](https://agent37.com) agent spend its time only on the filings that matter. You get a three-line brief on Telegram for those, and nothing for the rest.

The question Tape is built to answer: **do the filings Jev calls material actually move their stocks more the next day?** The evaluator measures that against two baselines (random, and a hand-written item-code rule).

## How it works

```
EDGAR feed -> clean text -> Jev (materiality, event, direction, surprise, boilerplate)
           -> router (now / digest / log) -> Agent37 agent writes the brief -> Telegram
```

See [architecture.md](architecture.md) for the full design and [deploy/agent37.md](deploy/agent37.md) to run it on Agent37.

## Quick start

```bash
pip install -e ".[dev,prices]"
export TYPESAFE_API_KEY=...                      # from the TypeSafe console
export TAPE_USER_AGENT="Tape you@example.com"    # the SEC requires a contact

tape init                # create the SQLite store
tape run                 # one pass: ingest new 8-Ks, score, route
tape pending             # filings waiting for a brief (what the agent reads)
tape brief ACC --text .. # save a brief the agent wrote
tape digest [--send]     # material filings from the last 24h (optionally to Telegram)
tape stats [--days 7]    # filings, scores, routes and Jev spend per day
tape backfill --days 90  # score past filings for the evaluation
tape evaluate            # prices, abnormal returns, metrics vs baselines
tape reroute             # re-apply tuned thresholds without re-scoring
tape dashboard           # writes out/dashboard.html
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `TYPESAFE_API_KEY` | none | Jev API key |
| `TAPE_USER_AGENT` | placeholder | Contact string sent to the SEC |
| `TAPE_DB_PATH` | `data/tape.db` | SQLite file; the cached SEC ticker map (refreshed daily) is kept in the same folder |
| `TAPE_JEV_MODEL` | `jev-latest` | Jev model name |
| `TAPE_MAX_CHARS` | `12000` | Filing characters sent to Jev |
| `TAPE_JEV_MAX_RETRIES` | `4` | Retries per filing on Jev timeouts, rate limits and 5xx errors; `0` disables |
| `TAPE_JEV_RETRY_BUDGET_S` | `60` | Most seconds spent on one filing, retries included, before it waits for the next pass |
| `TAPE_BRIEF_NOW`, `TAPE_DIGEST`, ... | see `tape/config.py` | Router thresholds |
| `TAPE_<THRESHOLD>__<EVENT>` | none | Per-event override, e.g. `TAPE_BRIEF_NOW__EARNINGS_GUIDANCE=4.5`; event labels are the keys of `EVENTS` in `tape/score.py` |

## Development

```bash
pytest -q
ruff check .
```
