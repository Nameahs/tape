"""A single static HTML page: what Jev flagged recently, and whether its scores have tracked real moves."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path

CSS = """
.viz-root { color-scheme: light; --surface-1:#fcfcfb; --surface-2:#f3f2ef; --rule:#e4e2dc;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --series-1:#2a78d6; }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) .viz-root {
  color-scheme: dark; --surface-1:#1a1a19; --surface-2:#242422; --rule:#353532;
  --text-primary:#f0efec; --text-secondary:#c3c2b7; --series-1:#3987e5; } }
:root[data-theme="dark"] .viz-root { color-scheme: dark; --surface-1:#1a1a19; --surface-2:#242422;
  --rule:#353532; --text-primary:#f0efec; --text-secondary:#c3c2b7; --series-1:#3987e5; }
* { box-sizing: border-box; }
body { margin: 0; }
.viz-root { background: var(--surface-1); color: var(--text-primary); min-height: 100vh;
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; padding: 24px 16px; }
main { max-width: 960px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 32px 0 8px; }
.muted { color: var(--text-secondary); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-top: 20px; }
.tile { background: var(--surface-2); border-radius: 8px; padding: 12px 14px; }
.tile b { display: block; font-size: 24px; font-variant-numeric: tabular-nums; }
.chart { width: 100%; max-width: 640px; height: auto; display: block; }
.chart .bar { fill: var(--series-1); } .chart .bar:hover { opacity: .8; }
.chart text { fill: var(--text-secondary); font-size: 12px; }
.chart .val { fill: var(--text-primary); font-weight: 600; }
.chart line { stroke: var(--rule); }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 10px 6px 0; border-bottom: 1px solid var(--rule); white-space: nowrap; }
th { color: var(--text-secondary); font-weight: 500; }
td.num, th.num { text-align: right; }
a { color: inherit; }
"""


def _tile(label: str, value: str, note: str = "") -> str:
    note_html = f'<span class="muted">{escape(note)}</span>' if note else ""
    return f'<div class="tile"><span class="muted">{escape(label)}</span><b>{escape(value)}</b>{note_html}</div>'


def _bar_chart(by_score: dict[str, dict[str, float]]) -> str:
    """Columns of mean |abnormal return| by score bucket, one series, labeled values."""
    keys = [k for k in ("1", "2", "3", "4", "5") if k in by_score]
    if not keys:
        return ""
    w, h, left, bottom, top = 640, 280, 48, 56, 24
    plot_w, plot_h = w - left - 16, h - top - bottom
    peak = max(by_score[k]["mean_abs_ar"] for k in keys) or 1.0
    step = plot_w / 5
    bar_w = step * 0.56
    parts = [f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" '
             'aria-label="Mean absolute next-day abnormal return by Jev materiality score">']
    for frac in (0.5, 1.0):
        y = top + plot_h * (1 - frac)
        parts.append(f'<line x1="{left}" x2="{w - 16}" y1="{y:.1f}" y2="{y:.1f}"/>'
                     f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{peak * frac:.1%}</text>')
    parts.append(f'<line x1="{left}" x2="{w - 16}" y1="{top + plot_h}" y2="{top + plot_h}"/>')
    for i, k in enumerate(("1", "2", "3", "4", "5")):
        cx = left + step * i + step / 2
        parts.append(f'<text x="{cx:.1f}" y="{top + plot_h + 18}" text-anchor="middle">{k}</text>')
        if k not in by_score:
            continue
        v, n = by_score[k]["mean_abs_ar"], by_score[k]["n"]
        bh = max(2.0, plot_h * v / peak)
        x, y = cx - bar_w / 2, top + plot_h - bh
        r = min(4.0, bh / 2)
        # Rounded top, square base on the axis.
        d = (f"M{x:.1f},{top + plot_h}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}"
             f"H{x + bar_w - r:.1f}Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + r:.1f}V{top + plot_h}Z")
        parts.append(f'<path class="bar" d="{d}"><title>Score {k}: {v:.2%} mean move, {n} filings</title></path>'
                     f'<text class="val" x="{cx:.1f}" y="{y - 6:.1f}" text-anchor="middle">{v:.1%}</text>')
    parts.append(f'<text x="{left + plot_w / 2:.1f}" y="{h - 4}" text-anchor="middle">Jev materiality score</text>')
    parts.append("</svg>")
    return "".join(parts)


def _recent(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT f.ticker, f.company, f.url, f.accepted_at, s.materiality, s.event, s.direction, b.route
        FROM filings f JOIN scores s USING (accession_no) LEFT JOIN briefs b USING (accession_no)
        WHERE julianday(f.accepted_at) >= julianday(?)
        ORDER BY s.materiality DESC LIMIT 25
        """,
        (since,),
    ).fetchall()


def build(conn: sqlite3.Connection, report: dict | None, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    since = (now - timedelta(hours=24)).isoformat()
    day = conn.execute(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(s.cost_usd), 0) AS cost,
               SUM(CASE WHEN b.route = 'now' THEN 1 ELSE 0 END) AS briefs
        FROM filings f JOIN scores s USING (accession_no) LEFT JOIN briefs b USING (accession_no)
        WHERE julianday(f.accepted_at) >= julianday(?)
        """,
        (since,),
    ).fetchone()

    tiles = [
        _tile("Filings scored, 24h", f"{day['n']:,}"),
        _tile("Briefed now", f"{day['briefs'] or 0:,}"),
        _tile("Jev cost, 24h", f"${day['cost']:.4f}"),
    ]
    eval_html = ('<p class="muted">No evaluation yet. Run <code>tape backfill</code> '
                 'then <code>tape evaluate</code>.</p>')
    if report:
        tiles.append(_tile("Top 10% / bottom 50%", f"{report['gap_ratio']:.2f}x",
                           f"random {report['random_gap_ratio']:.2f}x"))
        tiles.append(_tile("Rank correlation", f"{report['spearman']:.2f}", f"random {report['random_spearman']:.2f}"))
        rows = "".join(
            f'<tr><td>{k}</td><td class="num">{v["n"]:,}</td><td class="num">{v["mean_abs_ar"]:.2%}</td></tr>'
            for k, v in sorted(report["by_score"].items())
        )
        eval_html = (
            f'<p class="muted">{report["n"]:,} filings with next-day prices. '
            'Higher scores should mean bigger moves.</p>'
            + _bar_chart(report["by_score"])
            + '<details><summary class="muted">Table view</summary><div class="scroll"><table>'
            '<tr><th>Score</th><th class="num">Filings</th><th class="num">Mean |abnormal return|</th></tr>'
            + rows + "</table></div></details>"
            + f'<p class="muted">Item-code rule ({report["rule_flagged"]:,} flags): lift {report["rule_lift"]:.2f}x. '
            f'Jev at the same alert volume: lift {report["jev_lift_same_budget"]:.2f}x.</p>'
        )

    recent = _recent(conn, since)
    recent_rows = "".join(
        f'<tr><td class="num">{r["materiality"]:.1f}</td><td>{escape(r["ticker"] or "")}</td>'
        f'<td><a href="{escape(r["url"])}">{escape(r["company"])}</a></td><td>{escape(r["event"])}</td>'
        f'<td>{escape(r["direction"])}</td><td>{escape(r["route"] or "")}</td></tr>'
        for r in recent
    ) or '<tr><td colspan="6" class="muted">Nothing scored in the last 24 hours.</td></tr>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tape dashboard</title><style>{CSS}</style></head>
<body><div class="viz-root"><main>
<h1>Tape</h1>
<p class="muted">SEC 8-K filings scored by Jev. Updated {now:%Y-%m-%d %H:%M} UTC.</p>
<div class="tiles">{"".join(tiles)}</div>
<h2>Does a higher score mean a bigger move?</h2>
{eval_html}
<h2>Highest-scored filings, last 24 hours</h2>
<div class="scroll"><table>
<tr><th class="num">Score</th><th>Ticker</th><th>Company</th><th>Event</th><th>Direction</th><th>Route</th></tr>
{recent_rows}
</table></div>
</main></div></body></html>
"""


def write(conn: sqlite3.Connection, out: Path, report_path: Path | None) -> Path:
    report = json.loads(report_path.read_text()) if report_path and report_path.exists() else None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(conn, report))
    return out
