async function readPublication(path) {
  const response = await fetch(path, {
    mode: "same-origin",
    redirect: "error",
    cache: "no-store"
  });
  if (!response.ok) throw new Error(`HTTP ${response.status} for ${path}`);
  return response;
}

async function verify() {
  const date = new URLSearchParams(location.search).get("date");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new Error("Specify the session with ?date=YYYY-MM-DD.");
  const receiptPath = `/api/daily-research/${date}/status`;
  const receipt = await (await readPublication(receiptPath)).json();
  if (receipt.status !== "completed") throw new Error(`The receipt records a ${receipt.status} run.`);
  if (receipt.session_date !== date || receipt.accepted !== 7 ||
    !/^securities-\d{8}T\d{6}Z$/.test(receipt.run_id) ||
    receipt.packet_key !== `portfolio-research/${receipt.run_id}.json`) {
    throw new Error("The receipt does not identify a complete seven-report packet for this date.");
  }
  const packetPath = `/api/portfolio-research/test/${receipt.run_id}`;
  const bytes = await (await readPublication(packetPath)).arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const hash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
  if (hash !== receipt.packet_sha256) throw new Error(
    "The downloaded packet differs from the receipt checksum.");
  const packet = JSON.parse(new TextDecoder().decode(bytes));
  if (packet.run_id !== receipt.run_id || packet.session_date !== date || packet
    .all_reports_available !== true ||
    Object.keys(packet.subjects).length !== 7) throw new Error(
    "The packet has a different run, date, or report count.");
  return [
    `Verified seven reports for ${date}`,
    `Receipt: ${location.origin}${receiptPath}`,
    `Packet: ${location.origin}${packetPath}`,
    `SHA-256: ${hash}`
  ].join("\n");
}

verify().catch(error => `Verification failed. ${error.message}`)
  .then(message => {
    document.getElementById("result").textContent = message;
  });
