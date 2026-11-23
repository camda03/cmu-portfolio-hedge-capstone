"""Assemble a saved analyst run into one offline HTML reading copy; no API calls."""

import argparse
from datetime import datetime, timezone
from html import escape
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from markdown_it import MarkdownIt

ROLES = [
    ("business", "Business & market fundamentals", "Parallel research"),
    ("valuation", "Valuation & expectations", "Parallel research"),
    ("catalysts", "Catalysts & announcements", "Parallel research"),
    ("geoeconomics", "Geoeconomics & policy", "Parallel research"),
    ("draft", "Preliminary forecast", "Provisional synthesis of the four research notes"),
    ("review", "Critic / review", "Separate review call, with its own research"),
    ("report", "Final forecast and rationale", "Final numerical decision after the critique"),
]
CSS = """
:root{color-scheme:light;--ink:#183344;--muted:#5c6b73;--accent:#086b6b;--line:#dbe3e5}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f5f5f0;color:var(--ink);font:16px/1.65 system-ui,sans-serif}
main{max-width:1000px;margin:auto;padding:32px 24px 60px}header{padding:0 4px 22px;border-bottom:2px solid var(--accent)}
h1,h2,h3{line-height:1.25}h1{font-size:2rem;margin:8px 0 14px}h2{font-size:1.4rem}h3{font-size:1.1rem}
.eyebrow{font-size:.75rem;letter-spacing:.11em;text-transform:uppercase;color:var(--accent);font-weight:750;margin:0}
.muted,.meta{color:var(--muted);font-size:.88rem}.meta{margin:8px 0 18px;overflow-wrap:anywhere}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:20px 0 12px}
.metric{padding:14px 18px;background:white;border:1px solid var(--line);border-radius:8px}.metric strong{display:block;font-size:1.65rem;font-variant-numeric:tabular-nums}
.notice{padding:12px 16px;background:#e7f0ee;border-left:3px solid var(--accent)}.warning{background:#fff2dc;border-left:3px solid #ac6a10;padding:10px 14px}
nav{display:flex;flex-wrap:wrap;gap:8px;margin:22px 0}nav a{padding:6px 12px;border:1px solid var(--line);border-radius:5px;background:white;text-decoration:none;font-size:.85rem}
a{color:var(--accent);overflow-wrap:anywhere}a:hover{text-decoration:underline}a:focus-visible,summary:focus-visible{outline:3px solid #cf8e26;outline-offset:3px}
section{background:white;padding:24px 30px;margin:22px 0;border:1px solid var(--line);border-radius:9px;scroll-margin-top:20px}
section>h2{margin:8px 0}article{max-width:84ch}article h1{font-size:1.4rem}article h2{font-size:1.2rem;margin-top:1.8em}article h3{margin-top:1.6em}
article p,article li{overflow-wrap:anywhere}article blockquote{margin-left:0;border-left:3px solid var(--line);padding-left:18px;color:var(--muted)}
table{border-collapse:collapse;display:block;overflow-x:auto;max-width:100%;font-size:.9rem;margin:18px 0}th,td{padding:9px 12px;border-bottom:1px solid var(--line);text-align:left}th{background:#f0f4f4}
pre{overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5f6;padding:14px;border-radius:5px;font-size:.8rem}code{font-family:ui-monospace,monospace}
details{border-top:1px solid var(--line);margin-top:22px;padding-top:14px}summary{cursor:pointer;color:var(--accent);font-weight:600}footer{font-size:.85rem;color:var(--muted)}
@media(max-width:640px){main{padding:22px 12px}section{padding:20px 17px}.metrics{grid-template-columns:1fr}h1{font-size:1.65rem}.metric strong{font-size:1.45rem}}
@media print{body{background:white;font-size:11pt}main{max-width:none;padding:0}nav,details{display:none}section{border:0;border-top:1px solid #bbb;border-radius:0;padding:18px 0;break-before:auto}h1,h2,h3{break-after:avoid}a{color:inherit}table{display:table;font-size:9pt}}
"""


# Return the supplied mapping or an empty one when the saved field has another type.
def as_dict(value):
    return value if isinstance(value, dict) else {}


# Read a saved JSON object and return a displayable error when it is unavailable.
def read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        return data, ""
    except (OSError, UnicodeError, ValueError) as error:
        return {}, f"{path.name} unavailable or invalid ({type(error).__name__})."


# Read optional report text for display, using an empty value when it cannot be read.
def read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


# Format a recorded time in US Eastern time for the run summary.
def stamp(value):
    try:
        date = (
            datetime.fromtimestamp(value, timezone.utc)
            if type(value) in (int, float)
            else datetime.fromisoformat(value)
        )
        date = date if date.tzinfo else date.replace(tzinfo=timezone.utc)
        return date.astimezone(ZoneInfo("America/New_York")).strftime("%b %d, %Y, %I:%M %p %Z")
    except (ValueError, TypeError, OverflowError, OSError):
        return "Not recorded"


# Format a finite numeric field or display the missing-value label.
def number(value, spec, prefix=""):
    return (
        prefix + format(value, spec)
        if type(value) in (int, float) and math.isfinite(value)
        else "Not recorded"
    )


# Build the HTML review page from the files saved for a research run.
def render_run(run):
    run = Path(run).resolve()
    if not run.is_dir():
        raise ValueError(f"Run folder does not exist: {run}")
    markdown = MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")
    forecast, forecast_error = read_json(run / "forecast.json")
    context = read_text(run / "context.txt")
    subject = forecast.get("subject") or next(
        (s[9:] for s in context.splitlines() if s.startswith("Subject: ")), "Analyst run"
    )
    cutoff = forecast.get("information_cutoff") or next(
        (s[21:] for s in context.splitlines() if s.startswith("Research cutoff UTC: ")), None
    )
    notes = {name: read_text(run / f"{name}.md") for name, _, _ in ROLES}
    reused = (
        read_json(run / "reuse.json")[0].get("stages", []) if (run / "reuse.json").exists() else []
    )
    final_present = bool(notes["report"] and forecast.get("status") in ("estimate", "abstain"))
    available = sum(bool(text.strip()) for text in notes.values())
    state = "Saved packet" if available == len(ROLES) and final_present else "Partial packet"
    final_state = "Final artifacts present" if final_present else "Final artifacts unavailable"
    output = [
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        f"<title>{escape(str(subject))} · Research review</title><style>{CSS}</style></head><body><main>",
        f'<header><p class="eyebrow">Buy-side research · saved run</p><h1>{escape(str(subject))}</h1>'
        f'<p class="meta">Run {escape(run.name)}<br>Research cutoff: {escape(stamp(cutoff))}</p>'
        f'<p class="notice">{state} · {available}/7 notes available · {final_state}. '
        "This page assembles saved files; it adds no new analysis or verification.</p>",
    ]
    if forecast.get("status") == "estimate":
        unit, prefix, label = (
            ("index points", "", "level")
            if forecast.get("instrument_type") == "index"
            else ("USD", "$", "price")
        )
        ref_label = (
            "IBKR last price"
            if (forecast.get("reference_observation") or {}).get("reference_kind") == "ibkr_last"
            else "Reference close"
        )
        metrics = [
            ("Expected price return", number(forecast.get("expected_return"), "+.2%")),
            (
                f"Expected end {label} · {unit}",
                number(forecast.get("expected_end_price"), ",.2f", prefix),
            ),
            (f"{ref_label} · {unit}", number(forecast.get("reference_price"), ",.2f", prefix)),
        ]
        output.append(
            '<div class="metrics">'
            + "".join(
                f'<div class="metric">{label}<strong>{value}</strong></div>'
                for label, value in metrics
            )
            + "</div>"
        )
        output.append(
            f'<p class="muted">{ref_label}: {escape(str(forecast.get("price_date", "not recorded")))} · '
            f"Forecast end: {escape(str(forecast.get('horizon_end', 'not recorded')))} · "
            f"{escape(str(forecast.get('horizon_sessions', '?')))} trading sessions. Subjective scenarios; not calibrated probabilities.</p>"
        )
    elif forecast.get("status") == "abstain":
        output.append(
            f'<p class="notice"><strong>No numerical forecast.</strong> {escape(str(forecast.get("reason", "Reason not recorded.")))}</p>'
        )
    if forecast_error:
        output.append(f'<p class="warning">{escape(forecast_error)}</p>')
    if reused:
        output.append(
            '<p class="notice">This benchmark reuses saved stages, marked below. Their token records describe the original requests, not new charges.</p>'
        )
    if final_present:
        output.append(
            f'<p><a href="#report"><strong>Read the final forecast and its reasoning</strong></a></p>'
            f'<p class="meta">Forecast issued: {escape(stamp(forecast.get("issued_at")))}</p>'
        )
    output.append(
        '</header><nav aria-label="Report sections">'
        + "".join(f'<a href="#{name}">{title}</a>' for name, title, _ in ROLES)
        + "</nav>"
    )
    output.append(
        '<p class="muted">Four specialists research in parallel. The reviewer receives all four notes and the preliminary forecast; the final forecaster receives those materials and the critique. The headline numbers come from the final forecast. Role labels identify separate calls, not necessarily different models.</p>'
    )
    for index, (name, title, phase) in enumerate(ROLES, 1):
        raw, error = read_json(run / f"{name}.json")
        meta, usage = as_dict(raw.get("response_metadata")), as_dict(raw.get("usage_metadata"))
        content = raw.get("content", [])
        actions = (
            [b for b in content if isinstance(b, dict) and b.get("type") == "web_search_call"]
            if isinstance(content, list)
            else []
        )
        unresolved = sum(b.get("status") != "completed" for b in actions)
        output.append(
            f'<section id="{name}"><p class="eyebrow">{index:02d} · {phase}</p><h2>{title}</h2>'
            f'<p class="meta">{name}.md · Model: {escape(str(meta.get("model", meta.get("model_name", "not recorded"))))} · '
            f"API status: {escape(str(meta.get('status', 'not recorded')))}</p>"
        )
        if name in reused:
            output.append(
                '<p class="notice">Reused unchanged from the comparison case; no new model request for this stage.</p>'
            )
        if name == "draft":
            output.append(
                '<p class="notice">Preliminary work. Use the <a href="#report">final forecast and rationale</a> for this run\'s numerical decision.</p>'
            )
        if error or unresolved:
            warning = (
                error
                or f"{unresolved} of {len(actions)} recorded web actions are not marked completed. See the saved trace below."
            )
            output.append(f'<p class="warning">{escape(warning)}</p>')
        output.append(
            "<article>"
            + (
                markdown.render(notes[name])
                if notes[name]
                else '<p class="warning">This note is missing, empty or unreadable.</p>'
            )
            + "</article>"
        )
        trace = [
            {
                "status": b.get("status", "not recorded"),
                **{
                    k: v
                    for k, v in as_dict(b.get("action")).items()
                    if k in ("type", "query", "queries", "url", "pattern")
                },
            }
            for b in actions
        ]
        output.append(
            f"<details><summary>Research record · {len(actions)} recorded web actions</summary>"
            f'<p class="muted">Request timestamp: {escape(stamp(meta.get("created_at")))}. This is not elapsed time.<br>'
            f"Input tokens: {number(usage.get('input_tokens'), ',.0f')} · Output tokens: {number(usage.get('output_tokens'), ',.0f')} · "
            f"Reasoning tokens (included in output): {number(as_dict(usage.get('output_token_details')).get('reasoning'), ',.0f')}.</p>"
            '<p class="muted">Action records are not a count of verified sources or billed tool calls. Raw model reasoning is not displayed.</p>'
            f"<pre>{escape(json.dumps(trace, indent=2, ensure_ascii=False))}</pre></details></section>"
        )
    for filename in ("monitoring_handoff.json", "reassessment_dispositions.json"):
        if (run / filename).is_file():
            output.append(
                f"<details><summary>Returned {filename}</summary><pre>{escape(read_text(run / filename))}</pre></details>"
            )
    for filename in ("context.txt", "prompts.md", "forecast.json"):
        output.append(
            f"<details><summary>Saved {filename}</summary><pre>{escape(read_text(run / filename) or 'File unavailable or empty.')}</pre></details>"
        )
    output.append(
        "<footer><p>Saved model research, assembled without another model call. Completion does not establish accuracy or comprehensive coverage. The final revision has not been independently rechecked.</p></footer></main></body></html>"
    )
    destination = run / "report.html"
    destination.write_text("\n".join(output), encoding="utf-8")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="Dated run folder containing the saved notes")
    print(render_run(parser.parse_args().run))
