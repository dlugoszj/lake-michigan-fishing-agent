// Fish Mich page: asks the server (POST /ask), reads the streamed agent events, and fills in the page.
const $ = (id) => document.getElementById(id);
let realToday = "";

const TOOL_ICONS = {list_ports: "📍", get_recent_reports: "📋", find_similar_weeks: "📅", get_current_conditions: "🌊"};

// Short, readable version of a tool call's arguments.
function summarize(input) {
  const parts = [];
  if (input.ports) parts.push(input.ports.join(", "));
  if (input.weeks_back) parts.push(input.weeks_back + " weeks");
  if (input.around_date) parts.push("±" + input.window_days + " days of " + input.around_date);
  if (input.surface_temp_f != null) parts.push("surface ≈" + input.surface_temp_f + " °F");
  if (input.species_group) parts.push(input.species_group.replaceAll("_", " "));
  return parts.join(" · ") || "all ports";
}

const kchars = (n) => (n >= 1000 ? (n / 1000).toFixed(1) + "k" : n) + " chars";

function addStep(e) {
  const steps = $("steps");
  if (steps.querySelector(".empty")) steps.innerHTML = "";
  const d = document.createElement("details");
  d.innerHTML =
    '<summary><span class="tool-icon"></span>' +
    '<span class="call"><b></b><span class="args"></span></span>' +
    '<span class="status"><span class="spinner"></span>running</span></summary>';
  d.querySelector(".tool-icon").textContent = TOOL_ICONS[e.name] || "🔧";
  d.querySelector(".call b").textContent = e.name;
  d.querySelector(".args").textContent = summarize(e.input);
  d.dataset.started = performance.now();
  steps.appendChild(d);
  return d;
}

function finishStep(d, e) {
  const secs = ((performance.now() - Number(d.dataset.started)) / 1000).toFixed(1);
  const status = d.querySelector(".status");
  status.innerHTML = '<span class="mark"></span><span class="meta"></span><span class="chev">▸</span>';
  const mark = status.querySelector(".mark");
  mark.textContent = e.is_error ? "✗" : "✓";
  mark.className = e.is_error ? "err" : "ok";
  status.querySelector(".meta").textContent = kchars(e.chars) + " · " + secs + " s";
  const pre = document.createElement("pre");
  pre.className = "result";
  pre.textContent = e.content;  // plain text, never HTML
  d.appendChild(pre);
}

function showError(msg) {
  const div = document.createElement("div");
  div.className = "error";
  div.textContent = msg;
  $("answer").appendChild(div);
}

function pill(label, value) {
  const span = document.createElement("span");
  span.className = "pill";
  span.innerHTML = "<b></b> ";
  span.querySelector("b").textContent = value;
  span.append(label);
  return span;
}

function setTotals(items) {
  const t = $("totals");
  t.replaceChildren(...items.map(([label, value]) => pill(label, value)));
}

function updateBacktest() {
  const t = $("today").value;
  const box = $("backtest");
  box.hidden = !(t && t !== realToday);
  box.textContent = box.hidden ? "" :
    "Backtest: the agent runs as if it were " + t + ". Reports after that date are hidden; live water and wind are replaced by that week's buoy summary.";
}

async function ask() {
  const question = $("question").value.trim();
  if (!question) return;
  $("ask").disabled = true;
  $("steps").innerHTML = '<div class="empty">Starting…</div>';
  $("answer").innerHTML = '<p class="empty">Working…</p>';
  setTotals([]);

  let open = null;  // the step row waiting for its result
  let spent = 0;
  try {
    const resp = await fetch("/ask", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({question, model: $("model").value, today: $("today").value || null}),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || ("HTTP " + resp.status));
    }
    // Read the Server-Sent Events stream: events are "data: {json}" separated by a blank line.
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      let cut;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const chunk = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        if (chunk.startsWith("data: ")) handle(JSON.parse(chunk.slice(6)));
      }
    }
  } catch (err) {
    $("answer").innerHTML = "";
    showError("Request failed: " + err.message);
  } finally {
    $("ask").disabled = false;
  }

  function handle(e) {
    if (e.type === "tool_call") open = addStep(e);
    else if (e.type === "tool_result" && open) { finishStep(open, e); open = null; }
    else if (e.type === "model_call") {
      spent += e.cost;
      setTotals([["model call " + e.turn + " done", "⋯"], ["so far", "$" + spent.toFixed(3)]]);
    }
    else if (e.type === "answer") {
      $("answer").innerHTML = DOMPurify.sanitize(marked.parse(e.text));
      $("answer").querySelectorAll("a").forEach((a) => { a.target = "_blank"; a.rel = "noopener"; });
      if (e.truncated) showError("The answer was cut off at the token limit.");
    }
    else if (e.type === "error") showError(e.message);
    else if (e.type === "done") {
      setTotals([
        ["model calls", String(e.model_calls)],
        ["in / out tokens", (e.input_tokens + e.cache_read_tokens + e.cache_write_tokens).toLocaleString() + " / " + e.output_tokens.toLocaleString()],
        ["cost", "$" + e.cost.toFixed(3)],
        ["time", Math.round(e.seconds) + " s"],
        ["model", $("model").value],
      ]);
    }
  }
}

async function init() {
  const m = await (await fetch("/models")).json();
  for (const id of m.models) {
    const o = document.createElement("option");
    o.value = o.textContent = id;
    if (id === m.default) o.selected = true;
    $("model").appendChild(o);
  }
  realToday = m.today;
  $("today").value = m.today;
  $("today").max = m.today;
  updateBacktest();
}

$("ask").addEventListener("click", ask);
$("today").addEventListener("change", updateBacktest);
$("question").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) ask(); });
init();
