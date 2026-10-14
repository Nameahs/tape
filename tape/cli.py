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

    return Edgar(settings.user_agent)


def _jev():
    from tape.score import default_client

    return default_client()


def cmd_init(args, settings, conn) -> int:
    print(f"store ready at {settings.db_path}")
    return 0


def cmd_run(args, settings, conn) -> int:
    from tape.pipeline import run_pass

    print(run_pass(conn, _edgar(settings), _jev(), settings).summary())
    return 0


def cmd_backfill(args, settings, conn) -> int:
    from tape.pipeline import backfill

    print(backfill(conn, _edgar(settings), _jev(), settings, args.days).summary())
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
    print(format_digest(db.digest_since(conn, since)))
    return 0


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
    d.set_defaults(fn=cmd_digest)
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
