// Read a finite numeric metric while preserving missing values.
const metricNumber = (value) => (value == null || value === "" || !Number.isFinite(Number(value)) ?
  null : Number(value));
// Describe a signed futures position as long, short, or flat.
const positionName = (n) => (n == null ? "Unavailable" : n === 0 ? "Flat" :
  `${Math.abs(n)} ${n < 0 ? "short" : "long"}`);
// Show the direction and dollar size of a portfolio contribution.
const signedMoney = (n, places = 2) => (n == null ? "—" : (n > 0 ? "+" : "") + money(n, places));
// Calculate cumulative return relative to the opening portfolio value.
const returnFrom = (value, start) => (start > 0 && value != null ? Number(value) / start - 1 : null);
const booked = b.futures[t.contract]?.contracts ?? 0;
const nav = metricNumber(b.portfolio_value_usd),
  benchmark = metricNumber(b.unhedged_value_usd);
const startNav = metricNumber(b.opening_value_usd),
  effect = metricNumber(b.hedge_after_fees_usd);
const multiplier = Number(d.policy.future.multiplier);
const unit = metricNumber(t.mes_unit_notional_usd);
const otherOverlay = Object.entries(b.futures)
  .filter(([contract]) => contract !== t.contract)
  .reduce((sum, [, leg]) => sum + leg.contracts * Number(leg.mark) * multiplier, 0);
const anchor = t.candidates.find((row) => metricNumber(row.net_beta) !== null);
const decisionNav = metricNumber(t.valuation_nav_usd);
const equityBeta =
  anchor && decisionNav > 0 && unit > 0 ?
  anchor.net_beta - (anchor.target_contracts * unit + otherOverlay) / decisionNav :
  null;
const bookedBeta = equityBeta == null ? null : equityBeta + (booked * unit + otherOverlay) / decisionNav;
const proposed = t.candidates.find((row) => row.target_contracts === target);
const scenarioNames = ["decline", "flat", "rally"].filter((name) => proposed?.scenarios?.[name]);
let compareMode = "difference",
  performanceMode = "returns";
// Format a calendar date without moving it across a timezone boundary.
const calendarDay = (s) =>
  new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC"
  }).format(
    new Date(s + "T12:00:00Z"),
  );
// Create a link to another section of the portfolio desk.
const linkPage = (key, text) => `<a class="textlink" href="#${key}">${esc(text)}</a>`;
// Render one overview metric with its label, value, and context.
const kpi = (label, value, hint, color = "") =>
  `<div class="kpi"><span>${esc(label)}</span><strong class="${color}">${esc(value)}</strong><small>${esc(hint)}</small></div>`;
// Display portfolio beta to three decimal places when it is available.
const formatBeta = (beta) => (beta == null ? "—" : num(beta, 3));
// Build the buttons for switching between measures and mark the active choice.
const segment = (name, options, selected) =>
  `<div class="seg" role="group" aria-label="${esc(name)}">` +
  options
  .map(([key, label]) =>
    `<button data-switch="${key}" aria-pressed="${key === selected}">${esc(label)}</button>`)
  .join("") +
  "</div>";

// Show how the selected hedge performs under each market scenario.
function stressPanel() {
  if (!proposed || !scenarioNames.length)
    return panel("<h2>Decision scenarios</h2><p>Scenario estimates are unavailable for this run.</p>");
  const rows = scenarioNames.map((key) => {
    const scenario = proposed.scenarios[key],
      total = metricNumber(scenario.total_pnl_usd),
      overlay = metricNumber(scenario.overlay_after_cost_usd);
    return {
      key,
      label: {
        decline: "Market decline",
        flat: "Flat market",
        rally: "Market rally"
      } [key],
      market: d.policy.scenarios[key].index_return,
      total,
      overlay,
      base: total == null || overlay == null ? null : total - overlay,
    };
  });
  const relative = compareMode === "difference";
  const scale = Math.max(
    1,
    ...rows.flatMap((r) => (relative ? [Math.abs(r.overlay ?? 0)] : [Math.abs(r.total ?? 0), Math.abs(r
      .base ?? 0)])),
  );

  // Scale a scenario bar and place losses and gains on opposite sides of zero.
  function bar(n, cls) {
    if (n == null) return "";
    const width = (Math.abs(n) / scale) * 48;
    return `<i class="trackbar ${cls}" style="${n < 0 ? "right" : "left"}:50%;width:${width}%"></i>`;
  }
  const marks = rows
    .map(
      (r) =>
      `<div class="stressrow"><div><strong>${esc(r.label)}</strong><small>S&amp;P 500 ${esc(pct(r.market))}</small></div>` +
      `<div class="track ${relative ? "single" : "paired"}" aria-hidden="true"><i class="zero"></i>` +
      (relative ? bar(r.overlay, r.overlay >= 0 ? "gain" : "cost") : bar(r.base, "unhedged") + bar(r
        .total, "selected")) +
      `</div><div class="amount">${relative ? signedMoney(r.overlay) : signedMoney(r.total)}${relative ? "" : `<small>${signedMoney(r.base)}</small>`}</div></div>`,
    )
    .join("");
  return panel(
    `<div class="row"><h2>Decision scenarios</h2>${segment(
      "Scenario display",
      [
        ["difference", "Difference"],
        ["totals", "Total P&L"],
      ],
      compareMode,
    )}</div>` +
    `<p class="note">Modeled at the decision marks for the ${esc(positionName(target).toLowerCase())} MES target, including its change fee.</p>` +
    `<div class="stressgraph">${marks}</div>` +
    (relative ?
      '<div class="legend"><i class="dot gain"></i>Protection gained<i class="dot cost"></i>Upside or fee cost</div>' :
      '<div class="legend"><i class="dot selected"></i>Target portfolio<i class="dot unhedged"></i>Unhedged portfolio</div>'
      ),
  );
}

// Display the booked futures position, its mark, and its signed exposure.
function exposureCard() {
  const bookedNotional = booked * unit,
    value = b.futures[t.contract]?.mark ?? d.market.quotes[t.contract]?.last;
  const contractMonth = d.policy.future.contract_month;
  const expiry = new Intl.DateTimeFormat("en-US", {
    month: "long",
    year: "numeric",
    timeZone: "UTC"
  }).format(
    new Date(contractMonth.slice(0, 4) + "-" + contractMonth.slice(4) + "-01T12:00:00Z"),
  );
  const targetNote = target !== booked ? `Astra target: ${positionName(target).toLowerCase()}.` : "";
  return (
    `<section class="position"><div class="pos-head"><h2>Current hedge</h2><span class="paper-tag">Paper book</span></div>` +
    `<div class="pos-number">${esc(positionName(booked))}<span> MES</span></div><p>Micro E-mini S&amp;P 500 · ${esc(expiry)}</p>` +
    `<dl><div><dt>Signed exposure</dt><dd>${signedMoney(bookedNotional, 0)}</dd></div><div><dt>Contract mark</dt><dd>${num(value, 2)} pts</dd></div></dl>` +
    `<div class="pos-foot"><span>${esc(targetNote || (m.action === "defer" ? "Decision deferred" : "Recorded position"))}</span>${linkPage("hedge", "View decision")}</div></section>`
  );
}

// Compare the hedged and unhedged books over the same observations.
function performancePanel() {
  const returns = performanceMode === "returns",
    // Choose returns or dollar values for the selected portfolio series.
    chartValues = (k) => hist.map((pt) => (returns ? returnFrom(pt[k], startNav) : metricNumber(pt[k])));
  const labels = hist.map((pt) => shortTime(pt.as_of));
  const hedgeReturn = returnFrom(nav, startNav),
    baseReturn = returnFrom(benchmark, startNav);
  const values = chart(
    [chartValues("portfolio_value_usd"), chartValues("unhedged_value_usd")],
    labels,
    returns ? pct : (n) => money(n, 0),
    returns ? "Hedged and unhedged paper returns since opening" : "Hedged and unhedged paper NAV",
    hist.map((pt) => Date.parse(pt.as_of)),
  );
  const fill = d.tracker.decisions.filter((e) => e.receipt?.status === "filled" && e.contract === t
    .contract).at(-1);
  const lastMark = b.futures[t.contract]?.marked_at;
  const noLaterMark = fill && lastMark && Date.parse(lastMark) <= Date.parse(fill.at);
  return panel(
    `<div class="row"><h2>Portfolio performance</h2>${segment(
      "Performance display",
      [
        ["returns", "Return"],
        ["value", "NAV"],
      ],
      performanceMode,
    )}</div>` +
    `<div class="perf-legend"><span><i class="dot selected"></i>Hedged book <strong>${returns ? pct(hedgeReturn) : money(nav, 2)}</strong></span>` +
    `<span><i class="dot unhedged"></i>Unhedged book <strong>${returns ? pct(baseReturn) : money(benchmark, 2)}</strong></span></div>` +
    values +
    `<p class="note">${noLaterMark ? "Latest paper valuation is at the fill. The journal has no subsequent MES mark yet. " : ""}Lines connect recorded valuations of the same equity holdings and starting cash. Performance includes paper fills and fees.</p>`,
  );
}

// Summarize the analyst and news evidence that informed the decision.
function intelligenceCard() {
  const F = a.map((s) => s.forecast);
  const estimates = F.filter((f) => f.status === "estimate").length;
  let latest = F.map((f) => f.information_cutoff)
    .filter(Boolean)
    .sort()
    .at(-1);
  return (
    `<section class="panel research-hub"><h2>Research behind the hedge</h2>` +
    `<a class="research-link" href="#analysts"><span class="source-icon reports-icon" aria-hidden="true">A</span><span><b>Equity and Index Research Team</b><small>${a.length} reports · ${estimates} estimates</small></span><span class="chevron" aria-hidden="true">›</span></a>` +
    `<p class="source-date">${latest ? "Latest cutoff " + esc(et(latest)) : "Cutoff unavailable"}</p>` +
    `<a class="research-link" href="#gdelt"><span class="source-icon analog-icon" aria-hidden="true">G</span><span><b>GDELT historical analogs</b><small>${g ? num(g.queries) + " stories · " + num(g.corpus) + " in history" : "Coverage gap"}</small></span><span class="chevron" aria-hidden="true">›</span></a>` +
    `<p class="source-date">${g ? "Cohort " + esc(et(g.window.start)) : esc(d.gdelt_gap)}</p></section>`
  );
}

// Bring portfolio values, the hedge, scenarios, and supporting research onto one page.
function overviewPage() {
  const asof = d.session_date || d.market.session_date || d.batch_date;
  const betaChange =
    bookedBeta == null ?
    "Estimate unavailable" :
    "Change " + (bookedBeta - equityBeta > 0 ? "+" : "") + num(bookedBeta - equityBeta, 3);
  const effectBps = effect == null || !(startNav > 0) ? null : (10000 * effect) / startNav;
  return (
    `<header class="page-heading"><h1>Portfolio overview</h1><p>Six-stock paper portfolio <span>·</span> ${esc(calendarDay(asof))}</p></header>` +
    `<div class="kpis">${kpi("Portfolio value", money(nav, 2), "USD · marked paper NAV")}` +
    `${kpi("Unhedged beta", formatBeta(equityBeta), "Same stock basket · decision marks")}` +
    `${kpi("Beta with booked hedge", formatBeta(bookedBeta), betaChange, "primary-ink")}` +
    `${kpi("Hedge contribution", signedMoney(effect, 2), effectBps == null ? "After recorded fees" : num(effectBps, 3) + " bp since opening", effect < 0 ? "cost-ink" : effect > 0 ? "gain-ink" : "")}</div>` +
    archiveWarning() +
    freshnessBanner() +
    `<div class="overview-grid">${stressPanel()}${exposureCard()}${performancePanel()}${intelligenceCard()}</div>` +
    `<div class="journal-shortcut"><span><b>Decision journal</b> · ${journal.length} recorded · ${journal.filter((row) => !row.outcome).length} awaiting a mark</span>${linkPage("journal", "Review outcomes")}</div>` +
    `<div class="origin-note">${esc(d.snapshot_note || (closing ? "Historical closing prices stand in for live quotes in this rehearsal." : "Position and returns use the paper ledger’s latest recorded marks."))}</div>`
  );
}
