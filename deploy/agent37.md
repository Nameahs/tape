# Deploying Tape on Agent37

Tape runs inside one Agent37 workspace. Python does the fetching and scoring; the hosted agent
only wakes up to write briefs. These steps assume the Basic plan or higher (needed for Telegram).

## 1. Workspace

In the workspace web terminal:

```bash
git clone https://github.com/Nameahs/tape.git && cd tape
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[prices]"
tape init
```

Add these as workspace secrets or to `~/.profile`:

| Variable | Value |
| --- | --- |
| `TYPESAFE_API_KEY` | from the TypeSafe console |
| `TAPE_USER_AGENT` | `Tape <your email>` (the SEC requires a contact) |
| `TAPE_DB_PATH` | `/workspace/tape/data/tape.db` (keep it on persistent storage) |

Smoke test: `tape run -v` should print a summary with `scored=` greater than zero during market hours.

## 2. Brief skill

Copy `skills/tape-brief.md` into the agent's skills folder so it can be invoked by name.

## 3. Telegram

Connect Telegram in the workspace's messaging settings so the agent can message you directly.
If you would rather send the digest from a script, create a bot with @BotFather, set
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, and use `tape digest --send`.

## 4. Schedules (all times ET)

| Schedule | Task given to the agent |
| --- | --- |
| 6:30 am, Mon to Fri | Run `tape backfill --days 4` (no briefs) |
| Every 5 min, 7:00 am to 8:00 pm, Mon to Fri | "Use the tape-brief skill." |
| 6:00 pm, Mon to Fri | "Use the tape-brief skill, including the evening digest." |
| 9:00 pm, Mon to Fri | Run `tape evaluate && tape dashboard` (no briefs) |

If Yahoo is down, `tape evaluate` prints `prices unavailable; no outcomes added`, reports on the
outcomes it already has and exits 0, so the dashboard still refreshes and the next night catches up.
Exit status 2 means a setup problem, such as the `prices` extra not being installed.

Most 5-minute runs find nothing pending and end after two commands, so they cost almost no
agent credit. Jev scoring costs cents per day.

The 6:30 am backfill is the safety net for the feed, which only holds the latest 100 entries. Filings
that scroll off between passes (overnight, or during an outage) are still in EDGAR's daily index, and
this picks them up. When nothing was missed it costs one request per day read and no Jev calls. Four
days reaches the previous Friday across a Monday holiday. A daily index is dated by filing date, so an
8-K accepted after 5:30 pm appears in the next business day's index and is picked up a day later.
A pass that thinks it missed something ends its summary with `gap`, and `tape stats` counts them.
A backfill that finds no daily index for a weekday logs a warning naming the date, ends its summary with
`missing_days=N` and is counted in `tape stats` too. On a market holiday that's expected. Otherwise the
next morning's window retries the day, and once a day has aged out of it, rerun with a wider `--days`.

A filing Jev rejected, or one EDGAR kept failing to serve, is given up on after three attempts and
`tape stats` counts it. `tape retry` lists them with the last error. Once the cause is fixed (a key, a
parsing bug, a filing EDGAR has since repaired), `tape retry --score` or `--fetch` clears them, optionally
just one with `--accession ACC`, and the next pass tries them again. A fetch is retried only when the feed
or a backfill sees the filing again, so for an older one follow with `tape backfill --days N`.

### Exit status of `tape run` and `tape backfill`

| Status | Meaning |
| --- | --- |
| 0 | Pass finished. `blocked` at the end of the summary means EDGAR blocked part of it; the rest was scored. `outage` means EDGAR failed to serve some filings (timeouts or 5xx); they stay queued for a later pass and the rest was scored. `gap` means filings may have scrolled off the feed before this pass. `missing_days=N` means a backfill found no daily index for N weekdays. |
| 2 | Bad configuration or command line. One line on stderr starting `error:` (or argparse's usage message) says what to fix; rerunning won't help until it's fixed. |
| 75 | EDGAR blocked the host or could not be reached. One line on stderr says which; try again later (a block usually lifts in about 10 minutes). A `tape run` whose feed failed has still scored the filings it had stored. The run is still recorded in `tape stats`. |
| other | A real failure. Rerun with `-v` for the traceback. |

`tape run` and `tape backfill` create the store if it's missing. Every other command exits with status
2 and `error: no Tape store at ...` instead, so a wrong `TAPE_DB_PATH` or working directory can't pass
for a quiet day. If you see that line, fix the variable; `tape init` is only for a genuinely new store.

## 5. Backfill (once)

```bash
tape backfill --days 90 -v
tape evaluate --days 140   # 90 business days is about 130 calendar days
tape dashboard
```

If the backfill exits with status 75, EDGAR blocked or dropped it partway. A summary ending in `outage`
with status 0 means a few filings were skipped without stopping it. Either way, wait about 10 minutes
and run the same command again: filings already stored are not fetched or scored twice.

Then read `out/eval.json` and tune thresholds with the `TAPE_*` variables. Apply new thresholds
to stored scores with `tape reroute` instead of re-scoring.

## Open item

Agent37's developer API isn't documented publicly in detail. This plan uses only what the product
page lists (scheduled jobs, web terminal, messaging). Confirm scheduled tasks can run shell
commands before relying on the 5-minute cadence; see the issue tracking this.
