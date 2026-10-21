"""output/evals/index.html: every saved report on one page - run history, the latest run's scores, and each
case with its reply and the reasons behind every check. Self-contained (no scripts, no internet).

Written after each eval run; to rebuild it alone: python -m evals.page"""

import json
import re
import sys
from datetime import datetime
from html import escape
from pathlib import Path

from .runner import load_cases

RATES = [
    ("Status", "status"),
    ("Category", "category"),
    ("Attacks contained", "attacks_contained"),
    ("Reply checks", "reply_checks"),
    ("Fields", "fields"),
    ("LLM judge", "judge"),
]
STAMP = re.compile(r"(\d{8}T\d{6}Z)")

CSS = """
:root { --bg: #f7f7f5; --card: #fff; --text: #1f2328; --muted: #656d76; --line: #d8dee4;
  --ok: #1a7f37; --ok-bg: #dafbe1; --bad: #cf222e; --bad-bg: #ffebe9; --wait: #9a6700; --wait-bg: #fff8c5; }
@media (prefers-color-scheme: dark) { :root { --bg: #0d1117; --card: #161b22; --text: #e6edf3; --muted: #8d96a0;
  --line: #30363d; --ok: #3fb950; --ok-bg: #12261e; --bad: #f85149; --bad-bg: #2d1416; --wait: #d29922;
  --wait-bg: #2b2111; } }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px 16px 48px; background: var(--bg); color: var(--text);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 32px 0 10px; }
.muted { color: var(--muted); }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; margin-top: 16px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 12px; }
.card b { display: block; font-size: 22px; font-variant-numeric: tabular-nums; }
.card.bad b { color: var(--bad); } .card.ok b { color: var(--ok); }
.scroll { overflow-x: auto; background: var(--card); border: 1px solid var(--line); border-radius: 8px; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 12px; color: var(--muted); font-weight: 600; white-space: nowrap; }
tr:last-child td { border-bottom: 0; }
td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
.pill { display: inline-block; padding: 0 8px; border-radius: 10px; font-size: 12px; font-weight: 600; }
.pass { color: var(--ok); background: var(--ok-bg); } .fail { color: var(--bad); background: var(--bad-bg); }
.pending { color: var(--wait); background: var(--wait-bg); }
details summary { cursor: pointer; color: var(--muted); }
pre { white-space: pre-wrap; word-break: break-word; margin: 6px 0; padding: 8px; border-radius: 6px;
  background: var(--bg); border: 1px solid var(--line); font: 12.5px/1.45 ui-monospace, Consolas, monospace; }
ul.checks { margin: 6px 0; padding-left: 18px; }
"""


def load_reports(directory: Path) -> list[dict]:
    """Every report, oldest first, with the run time taken from its file name."""
    reports = []
    for path in directory.glob("report-*.json"):
        match = STAMP.search(path.name)
        if not match:
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        report["time"] = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
        reports.append(report)
    return sorted(reports, key=lambda report: report["time"])


def write_page(directory: Path) -> Path:
    reports = load_reports(directory)
    path = directory / "index.html"
    path.write_text(render(reports), encoding="utf-8")
    return path


def render(reports: list[dict]) -> str:
    body = "<h1>Eval results</h1><p class='muted'>No eval runs yet.</p>"
    if reports:
        latest = reports[-1]
        summary = latest["summary"]
        body = (
            "<h1>Eval results</h1>"
            f"<p class='muted'>Latest run {_when(latest)} &middot; {escape(summary['model'])} &middot; "
            f"{summary['cases']} cases, {summary['pending']} pending</p>"
            + _cards(summary)
            + "<h2>Cases in the latest run</h2>"
            + _cases(latest["records"])
            + _probes(latest["records"])
            + _confusion(summary)
            + "<h2>All runs</h2>"
            + _history(reports)
        )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>Eval results</title><style>{CSS}</style></head><body><main>{body}</main></body></html>"
    )


def _when(report: dict) -> str:
    return report["time"].strftime("%Y-%m-%d %H:%M") + " UTC"


def _rate(rate: dict) -> tuple[str, float | None]:
    if not rate.get("total"):
        return "-", None
    return f"{rate['passed']}/{rate['total']}", rate["passed"] / rate["total"]


def _cards(summary: dict) -> str:
    cards = []
    for label, key in RATES:
        text, share = _rate(summary[key])
        tone = "" if share is None else ("ok" if share == 1 else "bad")
        extra = ""
        if key == "judge" and summary["judge"].get("mean_score") is not None:
            extra = f"mean score {summary['judge']['mean_score']}"
        cards.append(f"<div class='card {tone}'><span class='muted'>{label}</span><b>{text}</b>{extra}</div>")
    judge = summary["judge"]
    if judge.get("probes"):
        trusted = judge["probes_agreed"] == judge["probes"]
        cards.append(
            f"<div class='card {'ok' if trusted else 'bad'}'><span class='muted'>Judge probes</span>"
            f"<b>{judge['probes_agreed']}/{judge['probes']}</b>{'reliable' if trusted else 'UNRELIABLE'}</div>"
        )
    return f"<div class='cards'>{''.join(cards)}</div>"


def _cases(records: list[dict]) -> str:
    subjects = {case["id"]: case.get("subject", "") for case in load_cases()}
    rows = []
    cases = [r for r in records if r["kind"] in ("case", "pending")]

    def failed(record: dict) -> bool:
        return any(not m["success"] and not m["skipped"] for m in record.get("metrics", {}).values())

    for record in sorted(cases, key=lambda r: (not failed(r), r["kind"] != "pending", r["id"])):
        subject = escape(subjects.get(record["id"], ""))
        if record["kind"] == "pending":
            rows.append(
                f"<tr><td><span class='pill pending'>pending</span></td><td>{record['id']}</td><td>{subject}</td>"
                f"<td colspan='3' class='muted'>{escape(record['reason'])}</td></tr>"
            )
            continue
        verdict = _pill(not failed(record), "pass", "fail")
        problems = [
            f"{escape(n)}: {escape(m['reason'] or '')}" for n, m in record["metrics"].items() if not m["success"]
        ]
        checks = "".join(_check(name, metric) for name, metric in record["metrics"].items())
        reply = f"<pre>{escape(record['reply'])}</pre>" if record.get("reply") else "<p class='muted'>No reply.</p>"
        flags = escape(", ".join(record.get("flags") or [])) or "none"
        rows.append(
            f"<tr><td>{verdict}</td><td>{record['id']}</td><td>{subject}"
            f"<details><summary>reply and checks</summary>{reply}<ul class='checks'>{checks}</ul>"
            f"<p class='muted'>Guardrail flags: {flags}</p></details></td>"
            f"<td>{escape(' / '.join(record['expected_status']))} &rarr; <b>{escape(record['status'])}</b></td>"
            f"<td>{escape(str(record.get('category') or '-'))}</td>"
            f"<td>{'<br>'.join(problems) or '<span class=muted>-</span>'}</td></tr>"
        )
    head = "<tr><th></th><th>ID</th><th>Subject</th><th>Status (expected &rarr; got)</th><th>Category</th>"
    return f"<div class='scroll'><table>{head}<th>Problems</th></tr>{''.join(rows)}</table></div>"


def _check(name: str, metric: dict) -> str:
    state = "pending" if metric["skipped"] else ("ok" if metric["success"] else "failed")
    score = "" if metric["score"] is None else f" ({metric['score']:.2f})"
    return f"<li><b>{escape(name)}</b> {state}{score} &mdash; {escape(metric['reason'] or '')}</li>"


def _pill(ok: bool, yes: str, no: str) -> str:
    return f"<span class='pill {'pass' if ok else 'fail'}'>{yes if ok else no}</span>"


def _probes(records: list[dict]) -> str:
    probes = [r for r in records if r["kind"] == "probe"]
    if not probes:
        return ""
    rows = []
    for probe in probes:
        metric = next(iter(probe["metrics"].values()))
        rows.append(
            f"<tr><td>{_pill(probe['agreed'], 'agreed', 'disagreed')}</td>"
            f"<td>{probe['id']}</td><td>{'good' if probe['expected_pass'] else 'bad'} reply</td>"
            f"<td class='num'>{metric['score'] if metric['score'] is not None else '-'}</td>"
            f"<td>{escape(metric['reason'] or '')}</td></tr>"
        )
    head = "<tr><th></th><th>ID</th><th>Labeled</th><th>Judge score</th><th>Judge's reason</th></tr>"
    return (
        "<h2>Judge probes</h2><p class='muted'>Hand-written replies with a known verdict: if the judge "
        f"disagrees, don't trust its scores.</p><div class='scroll'><table>{head}{''.join(rows)}</table></div>"
    )


def _confusion(summary: dict) -> str:
    cells = summary.get("confusion") or []
    if not cells:
        return ""
    rows = "".join(
        f"<tr><td>{_pill(c['expected'] == c['got'], 'ok', 'wrong')}</td><td>{escape(c['expected'])}</td>"
        f"<td>{escape(c['got'])}</td><td class='num'>{c['count']}</td></tr>"
        for c in sorted(cells, key=lambda c: (c["expected"] == c["got"], c["expected"]))
    )
    head = "<tr><th></th><th>Expected category</th><th>Got</th><th>Cases</th></tr>"
    return f"<h2>Categories</h2><div class='scroll'><table>{head}{rows}</table></div>"


def _history(reports: list[dict]) -> str:
    rows = []
    for report in reversed(reports):
        s = report["summary"]
        cells = "".join(f"<td class='num'>{_rate(s[key])[0]}</td>" for _, key in RATES)
        rows.append(
            f"<tr class=\"run\"><td class='num'>{_when(report)}</td><td>{escape(s['model'])}</td>"
            f"<td class='num'>{s['cases']}</td><td class='num'>{s['pending']}</td>{cells}</tr>"
        )
    head = "".join(f"<th>{label}</th>" for label, _ in RATES)
    return (
        "<div class='scroll'><table><tr><th>Run</th><th>Model</th><th>Cases</th><th>Pending</th>"
        f"{head}</tr>{''.join(rows)}</table></div>"
    )


if __name__ == "__main__":
    print(write_page(Path(sys.argv[1] if len(sys.argv) > 1 else "output/evals")))
