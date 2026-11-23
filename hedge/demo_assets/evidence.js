const quality = d.freshness?.rows || [];
const warnings = quality.filter((row) => !["Current", "Replay close", "Reference", "Dated"].includes(row
  .status));
const journal = d.history || [];
const prettyDate = (value) => (!value ? "—" : /^\d{4}-\d{2}-\d{2}$/.test(value) ? calendarDay(value) :
  et(value));

function archiveWarning() {
  const now = Date.now(),
    f = d.freshness;
  if (
    !sample &&
    f &&
    (closing ? now >= Date.parse(f.next_open) : now - Date.parse(d.as_of) > f.max_quote_age_seconds *
      1000)
  )
    return (
      '<p class="notice"><b>Archived prices.</b> This snapshot is dated ' +
      esc(calendarDay(f.session)) +
      ". " +
      (closing ? "A newer NYSE session has opened." : "The live quote age limit has elapsed.") +
      "</p>"
    );
  return "";
}

function freshnessBanner() {
  if (!quality.length)
    return '<div class="warning-strip">Source dates unavailable. ' + linkPage("freshness",
      "Inspect inputs") + "</div>";
  const note = warnings.length ?
    warnings
    .slice(0, 2)
    .map(
      (row) =>
      (row.group === "GDELT" ? "GDELT news cohort" : row.name) +
      " · " +
      row.status.toLowerCase() +
      (row.observed ? " (" + prettyDate(row.observed) + ")" : ""),
    )
    .join(" / ") :
    "All dated inputs met the checks for this review.";
  return (
    '<div class="' +
    (warnings.length ? "warning-strip" : "quality-summary") +
    '"><span><b>Source freshness</b> &nbsp;' +
    esc(note) +
    (warnings.length > 2 ? " · " + (warnings.length - 2) + " more" : "") +
    "</span>" +
    linkPage("freshness", "See source dates") +
    "</div>"
  );
}

function freshnessPage() {
  const grouped = [...new Set(quality.map((row) => row.group))];
  return (
    title(
      "Source dates & freshness",
      "Times are Eastern. Flags compare each input with the session under review. An archived session keeps its original flags.",
    ) +
    archiveWarning() +
    (d.market.price_mode === "last_close_rehearsal" ?
      '<p class="notice">Closing-price rehearsal for ' +
      esc(d.market.session_date) +
      ". Each instrument has a market timestamp and a separate retrieval time.</p>" :
      "") +
    grouped
    .map((group) =>
      panel(
        "<h2>" +
        esc(group) +
        "</h2>" +
        table(
          ["Source", "Status", "Information / market time", "Issued / received", "Forecast through",
            "Assessment"
          ],
          quality
          .filter((row) => row.group === group)
          .map((row) => [
            row.name,
            row.status,
            prettyDate(row.observed),
            prettyDate(row.received),
            prettyDate(row.horizon),
            row.reason,
          ]),
        ),
      ),
    )
    .join("") +
    (quality.length ? "" : panel('<p class="empty">The saved run has no dated evidence ledger.</p>'))
  );
}

function journalPage() {
  const scored = journal.filter((row) => row.outcome);
  const helped = scored.filter((row) => row.outcome.result === "Helped").length,
    cost = scored.filter((row) => row.outcome.result === "Cost").length;
  return (
    title(
      "Decision journal",
      "The forward record pairs each timestamped Astra decision with market observations collected after it was recorded. A subsequent review can use only the entries and marks available at its own cutoff.",
    ) +
    (d.history_as_of ? '<p class="note">Journal as of ' + esc(et(d.history_as_of)) + "</p>" : "") +
    stats(
      ["Decisions", journal.length, "Recorded Astra reviews"],
      ["Pending forward outcome", journal.length - scored.length,
        "First post-decision mark, then a later session"
      ],
      ["Outcome vs keeping the hedge", helped + " helped / " + cost + " cost", scored.length - helped -
        cost + " neutral"
      ],
    ) +
    (journal.length ?
      journal
      .map((row) => {
        const o = row.outcome,
          start = row.study_start;
        return panel(
          '<div class="row"><h2>' +
          esc(calendarDay(row.session)) +
          " · " +
          esc(row.action) +
          '</h2><span class="state ' +
          (o ? "ok" : "info") +
          '">' +
          esc(o ? o.result + " vs prior position" : "Pending outcome") +
          "</span></div>" +
          '<p class="note">' +
          esc(row.basis) +
          " · " +
          esc(row.contract) +
          " · " +
          esc(positionName(row.previous)) +
          " → " +
          esc(positionName(row.target)) +
          " · " +
          esc(row.time ? et(row.time) : "Decision time unavailable") +
          "</p>" +
          '<p class="measure">' +
          esc(row.rationale) +
          "</p>" +
          (o ?
            table(
              [
                "Forward entry",
                "Entry observed",
                "Follow-up",
                "Follow-up observed",
                "Versus keeping position",
                "Versus zero futures",
              ],
              [
                [
                  num(start.price, 2),
                  prettyDate(start.observed),
                  num(o.exit_mark, 2),
                  prettyDate(o.observed),
                  signedMoney(Number(o.versus_keep_usd)),
                  signedMoney(Number(o.versus_unhedged_usd)),
                ],
              ],
            ) :
            '<p class="note">Reference ' +
            esc(num(row.reference_price, 2)) +
            " points · change fee " +
            money(row.fee_usd, 2) +
            ". " +
            (start ?
              "Forward entry " +
              esc(num(start.price, 2)) +
              " points at " +
              esc(prettyDate(start.observed)) +
              ". Awaiting a later session." :
              "Awaiting the first eligible post-decision mark.") +
            "</p>") +
          (o?.alternatives ?
            details(
              "Other targets available at the decision",
              table(
                ["MES target", "Forward P&L vs keeping position"],
                Object.entries(o.alternatives).map(([key, value]) => [
                  signed(Number(key)),
                  signedMoney(Number(value)),
                ]),
              ),
            ) :
            "") +
          details(
            "Decision record",
            memo("Response", row.id) +
            (row.model ? memo("Model", row.model) : "") +
            (row.prompt_sha256 ? memo("Prompt SHA-256", row.prompt_sha256) : "") +
            memo("Analyst batch", row.analyst_run) +
            memo("GDELT study", row.gdelt_run_id) +
            (row.countercase ? memo("Countercase recorded at decision", row.countercase) : "") +
            (row.reconsider_if ? memo("Reconsider if", row.reconsider_if) : "") +
            (o ? memo("Follow-up source", o.mark_basis + " · received " + prettyDate(o
              .available)) : ""),
          ),
        );
      })
      .join("") :
      panel('<p class="empty">This snapshot contains no archived Astra decisions.</p>')) +
    panel(
      '<h2>How decisions are scored</h2><p class="note">Both scoring prices must follow the recorded decision. The first eligible IBKR observation sets the study entry. The first observation in a later session on the same expiry sets the exit. The comparison includes the modeled change fee; study marks do not guarantee a fill. Keeping the previous position measures the decision to change it. Zero futures measures the full overlay. Live advisory studies are hypothetical. The Portfolio view uses recorded paper marks and fills. Historical analog returns and stress scenarios are excluded from the forward record.</p>',
    )
  );
}
