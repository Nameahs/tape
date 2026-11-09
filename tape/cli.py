"""`tape` command line."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta

from tape import db
from tape.config import ConfigError, Settings, require_contact
from tape.files import write_atomic

log = logging.getLogger(__name__)

# sysexits.h EX_TEMPFAIL: the scheduler should simply try again later.
EXIT_TEMPFAIL = 75


def _edgar(settings: Settings):
    from tape.edgar import Edgar

    # Only commands that talk to EDGAR need a declared contact; stats, digest and pending don't.
    return Edgar(require_contact(settings.user_agent), cache_dir=settings.db_path.parent)


def _jev(settings: Settings):
    from tape.score import default_client, retry_policy

    return default_client(retry_policy(settings.jev_max_retries, settings.jev_retry_budget_s))


def cmd_init(args, settings, conn) -> int:
    print(f"store ready at {settings.db_path}")
    return 0


def _edgar_blocked(e: Exception) -> int:
    """Report an EDGAR block in one line. The agent reads this output, and a traceback would make
    "try again in ten minutes" look like a bug; the traceback stays in the log under -v."""
    log.info("EDGAR blocked the pass", exc_info=e)
    print("EDGAR is blocking requests from this host; try again in about 10 minutes", file=sys.stderr)
    return EXIT_TEMPFAIL


def _edgar_failed(e: Exception) -> int:
    """Same for an EDGAR request that still failed after Edgar.get's retries: a timeout, or a feed
    or daily index answering 5xx. Jev failures never get here; score_and_route handles those per
    filing, so any httpx error escaping a pass came from EDGAR."""
    log.info("EDGAR request failed", exc_info=e)
    print(f"EDGAR request failed ({e}); try again later", file=sys.stderr)
    return EXIT_TEMPFAIL


def cmd_run(args, settings, conn) -> int:
    import httpx

    from tape.edgar import EdgarBlockedError
    from tape.pipeline import run_pass

    try:
        stats = run_pass(conn, _edgar(settings), _jev(settings), settings)
    except EdgarBlockedError as e:
        return _edgar_blocked(e)
    except httpx.HTTPError as e:
        return _edgar_failed(e)
    print(stats.summary())
    return 0


def cmd_backfill(args, settings, conn) -> int:
    import httpx

    from tape.edgar import EdgarBlockedError
    from tape.pipeline import backfill

    try:
        stats = backfill(conn, _edgar(settings), _jev(settings), settings, args.days)
    except EdgarBlockedError as e:
        # The run is already recorded with its error; the days before the block stay scored.
        return _edgar_blocked(e)
    except httpx.HTTPError as e:
        return _edgar_failed(e)
    print(stats.summary())
    return 0


def format_reroute(report) -> str:
    """One line per move ("digest -> now: 12"), most moved first, then the routes every scored filing is on."""
    from tape.route import DIGEST, LOG, NOW

    lines = [
        f"{old or 'new'} -> {new}: {n}"
        for (old, new), n in sorted(report.moves.items(), key=lambda kv: (-kv[1], kv[0][0] or "", kv[0][1]))
    ]
    if not lines:
        lines.append("no routes changed")
    order = [NOW, DIGEST, LOG]
    totals = ", ".join(f"{r} {report.totals[r]}" for r in order if report.totals[r])
    lines.append(f"{report.changed} changed; routes now: {totals or 'no scored filings'}")
    return "\n".join(lines)


def cmd_reroute(args, settings, conn) -> int:
    from tape.pipeline import rescore_routes

    report = rescore_routes(conn, settings, dry_run=args.dry_run)
    if args.dry_run:
        print("dry run, nothing written")
    print(format_reroute(report))
    return 0


def cmd_pending(args, settings, conn) -> int:
    """Filings routed 'now' that still need a brief, as JSON for the agent. Only filings accepted in the
    last --max-age-hours are listed, so a backfill or reroute doesn't send old filings out as news.
    """
    since = (datetime.now(UTC) - timedelta(hours=args.max_age_hours)).isoformat()
    rows = [dict(r) for r in db.pending_briefs(conn, since)]
    print(json.dumps(rows, indent=2))
    return 0


def cmd_brief(args, settings, conn) -> int:
    text = (args.text if args.text is not None else sys.stdin.read()).strip()
    if not text:
        # Saving '' would mark the filing briefed and sent, and it would drop out of `pending`.
        print(f"error: empty brief for {args.accession_no}; nothing saved", file=sys.stderr)
        return 1
    sent_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with db.transaction(conn):
            db.save_brief(conn, args.accession_no, text, sent_at)
    except KeyError:
        # A mistyped accession number, or one never routed: the agent reads stderr, so one line, no traceback.
        print(f"error: no routed filing {args.accession_no}; nothing saved", file=sys.stderr)
        return 1
    print(f"saved brief for {args.accession_no}")
    return 0


def format_digest(rows) -> str:
    if not rows:
        return "Tape digest: nothing material today."
    lines = [f"Tape digest: {len(rows)} filings worth a look"]
    for r in rows:
        name = r["ticker"] or r["company"]
        lines.append(f"{r['materiality']:.1f}  {name}  {r['event']} ({r['direction']})  {r['url']}")
    return "\n".join(lines)


def cmd_digest(args, settings, conn) -> int:
    since = (datetime.now(UTC) - timedelta(hours=args.hours)).isoformat()
    text = format_digest(db.digest_since(conn, since))
    # Print first so the digest reaches the job log even when delivery fails.
    print(text)
    if args.send:
        import os

        import httpx

        from tape.notify import send_telegram

        missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.environ.get(k)]
        if missing:
            log.error("digest not sent: %s not set", " and ".join(missing))
            return 1
        try:
            send_telegram(text)
        except httpx.HTTPError as e:
            log.error("digest not sent: Telegram request failed: %s", e)
            return 1
    return 0


def cmd_stats(args, settings, conn) -> int:
    from tape.stats import collect, format_stats

    print(format_stats(collect(conn, args.days)))
    return 0


def format_given_up(score_rows, fetch_rows) -> str:
    if not score_rows and not fetch_rows:
        return "nothing given up"
    lines = []
    for kind, rows in (("score", score_rows), ("fetch", fetch_rows)):
        for r in rows:
            lines.append(f"{kind}  {r['accession_no']}  attempts={r['attempts']}  {r['last_attempt_at']}  "
                         f"{r['last_error']}")
    return "\n".join(lines)


def cmd_retry(args, settings, conn) -> int:
    """List the filings scoring or fetching gave up on, or with --score/--fetch put them back in the queue.
    Only the store changes: the next `tape run` (or backfill, for fetches) does the actual retrying.
    """
    if not (args.score or args.fetch):
        score_rows = [r for r in db.given_up(conn) if args.accession in (None, r["accession_no"])]
        fetch_rows = db.fetch_given_up(conn, None if args.accession is None else [args.accession])
        print(format_given_up(score_rows, fetch_rows))
        return 0
    with db.transaction(conn):
        scores = db.retry_given_up(conn, args.accession) if args.score else 0
        fetches = db.retry_fetch_given_up(conn, args.accession) if args.fetch else 0
    if args.accession is not None and not (scores or fetches):
        # Most likely a typo; the agent reads stderr, so say so rather than report "cleared 0".
        print(f"error: {args.accession} is not a given-up filing; nothing cleared", file=sys.stderr)
        return 1
    parts = [f"{scores} to score" if args.score else "", f"{fetches} to fetch" if args.fetch else ""]
    print(f"cleared {', '.join(p for p in parts if p)}; the next pass retries them")
    return 0


def cmd_evaluate(args, settings, conn) -> int:
    from pathlib import Path

    from tape.evaluate import (
        PRICE_LOOKBACK_DAYS,
        NotEnoughOutcomes,
        PricesUnavailable,
        YFinancePrices,
        compute_outcomes,
        evaluate,
        format_report,
        load_frame,
    )

    if not args.skip_prices:
        try:
            added = compute_outcomes(conn, YFinancePrices(), lookback_days=args.days or PRICE_LOOKBACK_DAYS)
        except PricesUnavailable as e:
            # Unpriced filings stay eligible for later runs, so carry on with what's stored: a Yahoo
            # outage shouldn't also stop the report and the `&& tape dashboard` that follows.
            log.warning("price download failed: %s", e)
            print("prices unavailable; no outcomes added")
        else:
            print(f"outcomes added: {added}")
    try:
        report = evaluate(load_frame(conn))
    except NotEnoughOutcomes as e:
        # Normal for the first weeks after deploy. Exit 0 so `tape evaluate && tape dashboard` still
        # refreshes the dashboard, and leave any earlier report in place rather than overwrite it.
        print(f"not enough outcomes to evaluate yet: {e.have} of {e.need}; report not written")
        return 0
    print(format_report(report))
    write_atomic(Path(args.out), report.to_json())
    return 0


def cmd_dashboard(args, settings, conn) -> int:
    from pathlib import Path

    from tape.dashboard import write

    print(f"wrote {write(conn, Path(args.out), Path(args.report))}")
    return 0


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tape", description="SEC 8-K watcher scored by Jev")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    # creates_store: only these commands may create a missing database. To the others an empty store
    # would look just like a quiet day, so a wrong TAPE_DB_PATH has to be an error (see main).
    sub.add_parser("init", help="create the SQLite store").set_defaults(fn=cmd_init, creates_store=True)
    # busy_exit: these commands take the pipeline lock, and this is their status if it's held.
    # A scheduled run that finds a backfill going is a quiet pass (0); a backfill someone started
    # by hand that didn't run should say so (1).
    r = sub.add_parser("run", help="one pass: ingest, score, route")
    r.set_defaults(fn=cmd_run, busy_exit=0, creates_store=True)
    b = sub.add_parser("backfill", help="ingest and score past business days")
    b.add_argument("--days", type=_positive_int, default=90)
    b.set_defaults(fn=cmd_backfill, busy_exit=1, creates_store=True)
    rr = sub.add_parser("reroute", help="re-apply thresholds to stored scores")
    rr.add_argument("--dry-run", action="store_true",
                    help="show which filings would change route without writing anything")
    # Reroute rewrites routes a running pass is also writing; --dry-run writes nothing, so it skips the lock.
    rr.set_defaults(fn=cmd_reroute, busy_exit=1, busy_note="nothing rerouted; try again when it finishes")
    pe = sub.add_parser("pending", help="recent filings waiting for a brief (JSON)")
    pe.add_argument("--max-age-hours", type=_positive_int, default=24,
                    help="skip filings EDGAR accepted longer ago than this (default 24)")
    pe.set_defaults(fn=cmd_pending)
    br = sub.add_parser("brief", help="save a brief written by the agent")
    br.add_argument("accession_no")
    br.add_argument("--text", help="brief text; read from stdin if omitted")
    br.set_defaults(fn=cmd_brief)
    d = sub.add_parser("digest", help="print the digest of recent material filings")
    d.add_argument("--hours", type=_positive_int, default=24)
    d.add_argument("--send", action="store_true", help="also send via Telegram Bot API")
    d.set_defaults(fn=cmd_digest)
    st = sub.add_parser("stats", help="filings, scores, routes and Jev spend per day")
    st.add_argument("--days", type=_positive_int, default=7)
    st.set_defaults(fn=cmd_stats)
    rt = sub.add_parser("retry", help="list filings Tape gave up on, or clear them to be retried")
    rt.add_argument("--score", action="store_true", help="clear filings scoring gave up on")
    rt.add_argument("--fetch", action="store_true", help="clear filings fetching gave up on")
    rt.add_argument("--accession", help="only this accession number")
    rt.set_defaults(fn=cmd_retry)
    e = sub.add_parser("evaluate", help="pull prices, compute abnormal returns, compare to baselines")
    e.add_argument("--skip-prices", action="store_true", help="use outcomes already stored")
    # Default lives in tape.evaluate; importing it here would load pandas for every command.
    e.add_argument("--days", type=_positive_int, default=None,
                   help="price filings accepted in the last N days (default 30; "
                        "raise it once after a backfill)")
    e.add_argument("--out", default="out/eval.json")
    e.set_defaults(fn=cmd_evaluate)
    ds = sub.add_parser("dashboard", help="write the static HTML dashboard")
    ds.add_argument("--out", default="out/dashboard.html")
    ds.add_argument("--report", default="out/eval.json")
    ds.set_defaults(fn=cmd_dashboard)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # At INFO httpx logs every request URL, and the Telegram token is part of the URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
        conn = db.connect(settings.db_path, create=getattr(args, "creates_store", False))
        try:
            busy_exit = getattr(args, "busy_exit", None)
            if busy_exit is None or getattr(args, "dry_run", False):
                return args.fn(args, settings, conn)
            return _run_locked(args, settings, conn, busy_exit)
        finally:
            conn.close()
    except ConfigError as e:
        # A typo in a workspace secret isn't a bug in the code, so it doesn't get a traceback unless asked.
        logging.getLogger(__name__).info("configuration error", exc_info=True)
        print(f"error: {e}", file=sys.stderr)
        return 2
    except db.StoreNotFound as e:
        print(f"error: no Tape store at {e}; check TAPE_DB_PATH, or run tape init to create one", file=sys.stderr)
        return 2


def _run_locked(args, settings, conn, busy_exit: int) -> int:
    """Run a pipeline command under the pipeline lock, so an overlapping run or backfill doesn't
    fetch and score (and pay Jev for) the same filings twice, and a reroute can't leave a running
    pass storing routes from the old thresholds or hold the store long enough to fail its writes."""
    from tape.lock import PipelineBusy, pipeline_lock

    try:
        with pipeline_lock(settings.db_path):
            return args.fn(args, settings, conn)
    except PipelineBusy:
        note = getattr(args, "busy_note", "skipping this pass")
        print(f"another tape run or backfill is in progress; {note}", file=sys.stderr)
        return busy_exit


if __name__ == "__main__":
    raise SystemExit(main())
