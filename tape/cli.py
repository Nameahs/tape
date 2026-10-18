"""`tape` command line."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta

from tape import db
from tape.config import Settings


def _edgar(settings: Settings):
    from tape.edgar import Edgar

    return Edgar(settings.user_agent, cache_dir=settings.db_path.parent)


def _jev(settings: Settings):
    from tape.score import default_client, retry_policy

    return default_client(retry_policy(settings.jev_max_retries, settings.jev_retry_budget_s))


def cmd_init(args, settings, conn) -> int:
    print(f"store ready at {settings.db_path}")
    return 0


def cmd_run(args, settings, conn) -> int:
    from tape.pipeline import run_pass

    print(run_pass(conn, _edgar(settings), _jev(settings), settings).summary())
    return 0


def cmd_backfill(args, settings, conn) -> int:
    from tape.pipeline import backfill

    print(backfill(conn, _edgar(settings), _jev(settings), settings, args.days).summary())
    return 0


def cmd_reroute(args, settings, conn) -> int:
    from tape.pipeline import rescore_routes

    print(dict(rescore_routes(conn, settings)))
    return 0


def cmd_pending(args, settings, conn) -> int:
    """Filings routed 'now' that still need a brief, as JSON for the agent."""
    rows = [dict(r) for r in db.pending_briefs(conn)]
    print(json.dumps(rows, indent=2))
    return 0


def cmd_brief(args, settings, conn) -> int:
    text = args.text if args.text is not None else sys.stdin.read()
    sent_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.transaction(conn):
        db.save_brief(conn, args.accession_no, text.strip(), sent_at)
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
    if args.send:
        from tape.notify import send_telegram

        send_telegram(text)
    print(text)
    return 0


def cmd_stats(args, settings, conn) -> int:
    from tape.stats import collect, format_stats

    print(format_stats(collect(conn, args.days)))
    return 0


def cmd_evaluate(args, settings, conn) -> int:
    from pathlib import Path

    from tape.evaluate import YFinancePrices, compute_outcomes, evaluate, format_report, load_frame

    if not args.skip_prices:
        print(f"outcomes added: {compute_outcomes(conn, YFinancePrices())}")
    report = evaluate(load_frame(conn))
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
    b.add_argument("--days", type=int, default=90)
    b.set_defaults(fn=cmd_backfill)
    sub.add_parser("reroute", help="re-apply thresholds to stored scores").set_defaults(fn=cmd_reroute)
    sub.add_parser("pending", help="filings waiting for a brief (JSON)").set_defaults(fn=cmd_pending)
    br = sub.add_parser("brief", help="save a brief written by the agent")
    br.add_argument("accession_no")
    br.add_argument("--text", help="brief text; read from stdin if omitted")
    br.set_defaults(fn=cmd_brief)
    d = sub.add_parser("digest", help="print the digest of recent material filings")
    d.add_argument("--hours", type=int, default=24)
    d.add_argument("--send", action="store_true", help="also send via Telegram Bot API")
    d.set_defaults(fn=cmd_digest)
    st = sub.add_parser("stats", help="filings, scores, routes and Jev spend per day")
    st.add_argument("--days", type=_positive_int, default=7)
    st.set_defaults(fn=cmd_stats)
    e = sub.add_parser("evaluate", help="pull prices, compute abnormal returns, compare to baselines")
    e.add_argument("--skip-prices", action="store_true", help="use outcomes already stored")
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
