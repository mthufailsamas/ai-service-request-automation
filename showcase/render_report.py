"""Render one self-contained, beginner-readable showcase report."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any


SUMMARY_FIELDS = {
    "integration_groups_passed",
    "integration_groups_total",
    "fictional_cases",
    "completed_cases",
    "rejected_cases",
    "analysis_attempts",
    "service_desk_records",
    "unfinished_work",
    "duplicate_terminal_effects",
}
CASE_FIELDS = {
    "case_reference",
    "subject",
    "request_type",
    "human_gate",
    "route",
    "final_state",
}


def escaped(value: Any) -> str:
    return html.escape(str(value), quote=True)


def validate_result(data: dict[str, Any]) -> None:
    if data.get("schema_version") != "showcase-v1":
        raise ValueError("The showcase evidence schema is unsupported.")
    runtime = data.get("runtime")
    summary = data.get("summary")
    cases = data.get("cases")
    controls = data.get("verified_controls")
    if not isinstance(runtime, dict) or set(runtime) != {
        "ai_provider",
        "hosted_or_paid_ai_calls",
        "data_scope",
    }:
        raise ValueError("The showcase runtime evidence is incomplete.")
    if runtime["ai_provider"] != "Controlled fixture":
        raise ValueError("The one-command showcase must use the fixture provider.")
    if runtime["hosted_or_paid_ai_calls"] != 0:
        raise ValueError("The one-command showcase must make 0 hosted AI calls.")
    if not isinstance(summary, dict) or set(summary) != SUMMARY_FIELDS:
        raise ValueError("The showcase summary fields changed.")
    if any(type(summary[field]) is not int for field in SUMMARY_FIELDS):
        raise ValueError("Every showcase summary value must be an integer.")
    if (
        summary["integration_groups_passed"] != 7
        or summary["integration_groups_total"] != 7
        or summary["fictional_cases"] != 7
        or summary["completed_cases"] != 6
        or summary["rejected_cases"] != 1
        or summary["unfinished_work"] != 0
        or summary["duplicate_terminal_effects"] != 0
    ):
        raise ValueError("The accepted lifecycle gate did not pass.")
    if not isinstance(cases, list) or len(cases) != 7:
        raise ValueError("Exactly 7 fictional case results are required.")
    for case in cases:
        if not isinstance(case, dict) or set(case) != CASE_FIELDS:
            raise ValueError("A showcase case result changed shape.")
        if case["final_state"] not in {"COMPLETED", "REJECTED"}:
            raise ValueError("Every showcase case must be terminal.")
        if any(not isinstance(case[field], str) or not case[field].strip() for field in CASE_FIELDS):
            raise ValueError("Showcase case text must be nonblank.")
    if not isinstance(controls, list) or len(controls) < 5:
        raise ValueError("The verified control list is incomplete.")
    if any(not isinstance(item, str) or not item.strip() for item in controls):
        raise ValueError("Verified control text must be nonblank.")


def render_report(data: dict[str, Any]) -> str:
    validate_result(data)
    summary = data["summary"]
    runtime = data["runtime"]
    case_rows = "".join(
        f"""
        <tr>
          <td><code>{escaped(case['case_reference'])}</code><strong>{escaped(case['subject'])}</strong></td>
          <td>{escaped(case['request_type'])}</td>
          <td>{escaped(case['human_gate'])}</td>
          <td>{escaped(case['route'])}</td>
          <td><span class="state {escaped(case['final_state'].lower())}">{escaped(case['final_state'])}</span></td>
        </tr>"""
        for case in data["cases"]
    )
    control_items = "".join(
        f"<li><span>✓</span>{escaped(item)}</li>"
        for item in data["verified_controls"]
    )
    generated_at = escaped(data.get("generated_at", "Unknown"))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Service Request Automation — Showcase Report</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #10233f;
      --muted: #5f6f86;
      --line: #dbe5ef;
      --panel: #ffffff;
      --canvas: #f4f7fb;
      --navy: #173f73;
      --teal: #0b8a80;
      --green: #18794e;
      --amber: #a15c00;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--canvas);
      color: var(--ink);
      font: 16px/1.55 Inter, "Segoe UI", Arial, sans-serif;
    }}
    .wrap {{ width: min(1180px, calc(100% - 32px)); margin: 0 auto; }}
    .hero {{
      padding: 64px 0 48px;
      color: white;
      background:
        radial-gradient(circle at 85% 5%, rgba(31, 194, 178, .34), transparent 35%),
        linear-gradient(135deg, #102d52, #173f73 58%, #0b6f71);
    }}
    .eyebrow {{ text-transform: uppercase; letter-spacing: .14em; font-size: 12px; font-weight: 800; opacity: .8; }}
    h1 {{ max-width: 780px; margin: 12px 0 14px; font-size: clamp(36px, 6vw, 66px); line-height: 1.03; }}
    .lead {{ max-width: 760px; margin: 0; color: #dbeaf8; font-size: 19px; }}
    .badges {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 28px; }}
    .badge {{ padding: 8px 12px; border: 1px solid rgba(255,255,255,.28); border-radius: 999px; background: rgba(255,255,255,.1); font-weight: 700; }}
    main {{ padding: 34px 0 64px; }}
    .grid {{ display: grid; gap: 18px; }}
    .metrics {{ grid-template-columns: repeat(4, 1fr); }}
    .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 18px; padding: 24px; box-shadow: 0 12px 32px rgba(20,45,75,.06); }}
    .metric strong {{ display: block; font-size: 34px; line-height: 1.1; color: var(--navy); }}
    .metric span {{ color: var(--muted); font-weight: 650; }}
    section {{ margin-top: 28px; }}
    h2 {{ margin: 0 0 8px; font-size: 28px; }}
    .section-intro {{ margin: 0 0 18px; color: var(--muted); }}
    .flow {{ grid-template-columns: repeat(6, 1fr); align-items: stretch; }}
    .step {{ position: relative; min-height: 156px; padding: 20px; border-top: 4px solid var(--teal); }}
    .step b {{ display: inline-grid; place-items: center; width: 30px; height: 30px; border-radius: 50%; color: white; background: var(--teal); }}
    .step h3 {{ margin: 14px 0 6px; font-size: 17px; }}
    .step p {{ margin: 0; color: var(--muted); font-size: 14px; }}
    .table-card {{ padding: 0; overflow: hidden; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 15px 17px; text-align: left; vertical-align: top; border-bottom: 1px solid var(--line); }}
    th {{ color: var(--muted); background: #f8fafc; font-size: 13px; text-transform: uppercase; letter-spacing: .05em; }}
    td:first-child strong {{ display: block; margin-top: 5px; }}
    code {{ color: var(--navy); font: 12px/1.3 Consolas, monospace; }}
    .state {{ display: inline-block; padding: 5px 9px; border-radius: 999px; font-size: 12px; font-weight: 800; }}
    .completed {{ color: var(--green); background: #e7f6ee; }}
    .rejected {{ color: var(--amber); background: #fff2dc; }}
    .two {{ grid-template-columns: 1.2fr .8fr; }}
    ul {{ list-style: none; padding: 0; margin: 0; }}
    li {{ display: flex; gap: 10px; padding: 9px 0; border-bottom: 1px solid var(--line); }}
    li:last-child {{ border-bottom: 0; }}
    li span {{ color: var(--green); font-weight: 900; }}
    .boundary {{ border-left: 5px solid #d99023; background: #fffaf1; }}
    .boundary strong {{ color: #7d4800; }}
    footer {{ padding: 26px 0 46px; color: var(--muted); font-size: 13px; }}
    @media (max-width: 900px) {{
      .metrics {{ grid-template-columns: repeat(2, 1fr); }}
      .flow {{ grid-template-columns: repeat(2, 1fr); }}
      .two {{ grid-template-columns: 1fr; }}
      .table-card {{ overflow-x: auto; }}
      table {{ min-width: 880px; }}
    }}
    @media (max-width: 520px) {{ .metrics, .flow {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <header class="hero">
    <div class="wrap">
      <div class="eyebrow">One-command controlled local showcase</div>
      <h1>AI Service Request Automation</h1>
      <p class="lead">A fictional service request travels through intake, AI-assisted structure, deterministic validation, human authority, orchestration, and downstream delivery.</p>
      <div class="badges">
        <span class="badge">7/7 lifecycle groups PASS</span>
        <span class="badge">Fixture AI • IDR 0</span>
        <span class="badge">0 unfinished work</span>
        <span class="badge">Disposable local runtime</span>
      </div>
    </div>
  </header>

  <main class="wrap">
    <div class="grid metrics">
      <div class="card metric"><strong>{summary['fictional_cases']}</strong><span>fictional lifecycle cases</span></div>
      <div class="card metric"><strong>{summary['completed_cases']}</strong><span>completed downstream</span></div>
      <div class="card metric"><strong>{summary['rejected_cases']}</strong><span>rejected and notified</span></div>
      <div class="card metric"><strong>{summary['service_desk_records']}</strong><span>Service Desk records</span></div>
    </div>

    <section>
      <h2>How this works in a company</h2>
      <p class="section-intro">In a company, this system stays active on a server; the showcase command reproduces the portfolio workflow in 1 controlled run.</p>
      <div class="grid flow">
        <div class="card step"><b>1</b><h3>Employee intake</h3><p>Portal, email, Teams, or webhook creates 1 durable request.</p></div>
        <div class="card step"><b>2</b><h3>AI proposal</h3><p>AI proposes a type, summary, and structured fields. Human and rule authority remain separate.</p></div>
        <div class="card step"><b>3</b><h3>Rule validation</h3><p>Schema, identifiers, evidence, permissions, and ownership are checked.</p></div>
        <div class="card step"><b>4</b><h3>Human gate</h3><p>Missing information, ambiguity, access, and data change reach the right person.</p></div>
        <div class="card step"><b>5</b><h3>Orchestration</h3><p>n8n moves durable commands between services with retry and replay protection.</p></div>
        <div class="card step"><b>6</b><h3>Delivery & evidence</h3><p>Service Desk receives the action; audit and operations retain the outcome.</p></div>
      </div>
    </section>

    <section>
      <h2>Fresh controlled lifecycle results</h2>
      <p class="section-intro">7 separate cases exercise the major branches automatically in 1 controlled run.</p>
      <div class="card table-card">
        <table>
          <thead><tr><th>Case</th><th>Type</th><th>Human or safety gate</th><th>Delivered route</th><th>Final state</th></tr></thead>
          <tbody>{case_rows}</tbody>
        </table>
      </div>
    </section>

    <section class="grid two">
      <div class="card">
        <h2>Verified controls</h2>
        <p class="section-intro">These checks completed in the same disposable run.</p>
        <ul>{control_items}</ul>
      </div>
      <div class="card boundary">
        <h2>Evidence boundary</h2>
        <p><strong>This is not production validation.</strong></p>
        <p>The showcase uses {escaped(runtime['ai_provider'])} and {escaped(runtime['data_scope'])}; hosted or paid AI calls: {runtime['hosted_or_paid_ai_calls']}.</p>
        <p>The separate real Ollama walkthrough previously reached <code>NEEDS_REVIEW</code> after an exact-schema mismatch. The locked v1 system quality remains <strong>CHECK</strong>, not PASS, and is not changed by this deterministic showcase.</p>
      </div>
    </section>
  </main>

  <footer class="wrap">
    Generated {generated_at}. The report is overwritten on every showcase run; disposable services and temporary evidence are removed automatically.
  </footer>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    document = render_report(data)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.tmp")
    try:
        temporary.write_text(document, encoding="utf-8")
        temporary.replace(args.output)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
