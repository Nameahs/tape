"""`tape` command line."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta

from tape import db
from tape.config import Settings, require_contact

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


def cmd_run(args, settings, conn) -> int:
    from tape.edgar import EdgarBlockedError
    from tape.pipeline import run_pass

    try:
        stats = run_pass(conn, _edgar(settings), _jev(settings), settings)
    except EdgarBlockedError as e:
        return _edgar_blocked(e)
    print(stats.summary())
    return 0


def cmd_backfill(args, settings, conn) -> int:
    from tape.edgar import EdgarBlockedError
    from tape.pipeline import backfill

    try:
        stats = backfill(conn, _edgar(settings), _jev(settings), settings, args.days)
    except EdgarBlockedError as e:
        # The run is already recorded with its error; the days before the block stay scored.
        return _edgar_blocked(e)
    print(stats.summary())
    return 0


def cmd_reroute(args, settings, conn) -> int:
    from tape.pipeline import rescore_routes

    print(dict(rescore_routes(conn, settings)))
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


def cmd_evaluate(args, settings, conn) -> int:
    from pathlib import Path

    from tape.evaluate import (
        PRICE_LOOKBACK_DAYS,
        NotEnoughOutcomes,
        YFinancePrices,
        compute_outcomes,
        evaluate,
        format_report,
        load_frame,
    )

    if not args.skip_prices:
        added = compute_outcomes(conn, YFinancePrices(), lookback_days=args.days or PRICE_LOOKBACK_DAYS)
        print(f"outcomes added: {added}")
    try:
        report = evaluate(load_frame(conn))
    except NotEnoughOutcomes as e:
        # Normal for the first weeks after deploy. Exit 0 so `tape evaluate && tape dashboard` still
        # refreshes the dashboard, and leave any earlier report in place rather than overwrite it.
        print(f"not enough outcomes to evaluate yet: {e.have} of {e.need}; report not written")
        return 0
    print(format_report(report))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.to_json())
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
    sub.add_parser("init", help="create the SQLite store").set_defaults(fn=cmd_init)
    sub.add_parser("run", help="one pass: ingest, score, route").set_defaults(fn=cmd_run)
    b = sub.add_parser("backfill", help="ingest and score past business days")
    b.add_argument("--days", type=_positive_int, default=90)
    b.set_defaults(fn=cmd_backfill)
    sub.add_parser("reroute", help="re-apply thresholds to stored scores").set_defaults(fn=cmd_reroute)
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
    settings = Settings.from_env()
    conn = db.connect(settings.db_path)
    try:
        return args.fn(args, settings, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
