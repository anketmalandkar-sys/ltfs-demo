"""Collects every case's scores during the run, then prints the summary and writes the JSON report."""

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from .metrics import allowed


class Report:
    def __init__(self):
        self.model = ""
        self.output_dir = Path("output/evals")
        self.records: list[dict] = []
        self.max_calls = 0
        self.budget = {"left": 0}  # shared with the CachedLLMs: what's left of --max-calls

    def add(self, case: dict, result, metrics: list, llm_calls: int) -> None:
        expect = case["expect"]
        self.records.append(
            {
                "id": case["id"],
                "kind": "case",
                "attack": bool(expect.get("attack")),
                "expected_status": allowed(expect["status"]),
                "status": result.status.value,
                "expected_category": allowed(expect["category"]) if "category" in expect else None,
                "category": result.triage.category.value if result.triage else None,
                "llm_calls": llm_calls,
                "flags": result.guardrail_flags,
                "reply": result.draft.body if result.draft else None,
                "metrics": {metric.__name__: _metric(metric) for metric in metrics},
            }
        )

    def add_probe(self, probe: dict, metric, agreed: bool) -> None:
        self.records.append(
            {"id": probe["id"], "kind": "probe", "expected_pass": probe["expect"]["judge_pass"], "agreed": agreed}
            | {"metrics": {metric.__name__: _metric(metric)}}
        )

    def add_pending(self, case: dict, reason: str) -> None:
        self.records.append({"id": case["id"], "kind": "pending", "reason": reason})

    # ------------------------------------------------------------ summary ----

    def summary(self) -> dict:
        cases = [r for r in self.records if r["kind"] == "case"]
        probes = [r for r in self.records if r["kind"] == "probe"]
        categorized = [r for r in cases if r["expected_category"] is not None]
        judged = [m for r in cases for name, m in r["metrics"].items() if "judge" in name and not m["skipped"]]

        def rate(name: str, rows: list[dict]) -> dict:
            scored = [r["metrics"][name] for r in rows if name in r["metrics"] and not r["metrics"][name]["skipped"]]
            return {"passed": sum(m["success"] for m in scored), "total": len(scored)}

        return {
            "model": self.model,
            "cases": len(cases),
            "pending": sum(r["kind"] == "pending" for r in self.records),
            "llm_calls": sum(r["llm_calls"] for r in cases),  # by the agent, cached or not
            "new_llm_calls": self.max_calls - self.budget["left"],  # sent this run, agent and judge
            "status": rate("Status", cases),
            "category": rate("Category", categorized),
            "attacks_contained": rate("Attack contained", cases),
            "reply_checks": rate("Reply checks", cases),
            "fields": rate("Fields", cases),
            "judge": {
                "passed": sum(m["success"] for m in judged),
                "total": len(judged),
                "mean_score": round(sum(m["score"] for m in judged) / len(judged), 2) if judged else None,
                "probes_agreed": sum(r["agreed"] for r in probes),
                "probes": len(probes),
            },
            "per_category": per_category(categorized),
            "confusion": confusion(categorized),
        }

    def text(self) -> str:
        s = self.summary()
        lines = [
            f"Evals: {s['cases']} cases with {s['model']}, {s['pending']} pending, "
            f"{s['llm_calls']} agent LLM requests ({s['new_llm_calls']} new requests sent this run)"
        ]
        for label, key in [
            ("Status", "status"),
            ("Category", "category"),
            ("Attacks contained", "attacks_contained"),
            ("Reply checks", "reply_checks"),
            ("Fields", "fields"),
        ]:
            lines.append(f"  {label:<20} {_fraction(s[key])}")
        judge = s["judge"]
        if judge["total"]:
            lines.append(f"  {'LLM judge':<20} {_fraction(judge)}, mean score {judge['mean_score']}")
        if judge["probes"]:
            trusted = "ok" if judge["probes_agreed"] == judge["probes"] else "UNRELIABLE"
            lines.append(f"  {'Judge probes':<20} {judge['probes_agreed']}/{judge['probes']} agreed ({trusted})")
        if s["per_category"]:
            lines.append(f"\n  {'category':<24} {'precision':>9} {'recall':>7}")
            for name, row in s["per_category"].items():
                lines.append(f"  {name:<24} {_pct(row['precision']):>9} {_pct(row['recall']):>7}")
        if s["confusion"]:
            lines.append("\n  Confusion (expected -> got)")
            for (want, got), count in sorted(s["confusion"].items()):
                lines.append(f"  {'ok ' if want == got else '!! '}{want:<24} -> {got:<24} {count}")
        if s["pending"]:
            lines.append(f"\n  {s['pending']} cases are pending (request limit or LLM quota). Run again to continue.")
        return "\n".join(lines)

    def save(self) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.output_dir / f"report-{self.model.replace('/', '_')}-{stamp}.json"
        summary = self.summary()
        summary["confusion"] = [{"expected": w, "got": g, "count": c} for (w, g), c in summary["confusion"].items()]
        path.write_text(json.dumps({"summary": summary, "records": self.records}, indent=2), encoding="utf-8")
        return path


def _metric(metric) -> dict:
    skipped = bool(getattr(metric, "skipped", False))
    return {"score": metric.score, "success": bool(metric.success), "reason": metric.reason, "skipped": skipped}


def _labels(row: dict) -> tuple[str, str]:
    """(expected, got). With several acceptable categories, a correct answer counts as the expected one."""
    got = row["category"] or "none"
    options = [option or "none" for option in row["expected_category"]]
    return (got if got in options else options[0]), got


def per_category(rows: list[dict]) -> dict:
    pairs = [_labels(row) for row in rows]
    out = {}
    for name in sorted({label for pair in pairs for label in pair}):
        hits = sum(want == got == name for want, got in pairs)
        predicted = sum(got == name for _, got in pairs)
        actual = sum(want == name for want, _ in pairs)
        out[name] = {"precision": hits / predicted if predicted else None, "recall": hits / actual if actual else None}
    return out


def confusion(rows: list[dict]) -> dict:
    return dict(Counter(_labels(row) for row in rows))


def _fraction(rate: dict) -> str:
    if not rate["total"]:
        return "-"
    return f"{rate['passed']}/{rate['total']} ({rate['passed'] / rate['total']:.0%})"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"
