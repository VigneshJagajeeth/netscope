/* NetScope dashboard - plain JavaScript, no build step. */

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtBytes = (n) => {
  if (n == null) return "-";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
};
const pill = (ok, yes = "OK", no = "FAIL") => `<span class="pill ${ok ? "pill-ok" : "pill-bad"}">${ok ? yes : no}</span>`;
const spin = (txt = "Running…") => `<p class="muted"><span class="spinner"></span>${esc(txt)}</p>`;

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2500);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    let detail = data?.detail ?? res.statusText;
    if (Array.isArray(detail)) detail = detail.map((d) => `${d.loc?.slice(-1)[0]}: ${d.msg}`).join("; ");
    const err = new Error(detail); err.status = res.status; throw err;
  }
  return data;
}

function table(headers, rows, rowAttrs = () => "") {
  if (!rows.length) return `<p class="muted">Nothing to show.</p>`;
  return `<table><thead><tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r, i) => `<tr ${rowAttrs(i)}>${r.map((c) => `<td>${c ?? ""}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

/* ------------------------------------------------------------------ nav */
const loaders = {};
let current = "overview";
function show(view) {
  current = view;
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  history.replaceState(null, "", `#${view}`);
  loaders[view]?.();
}
document.querySelectorAll("#nav a").forEach((a) => a.addEventListener("click", () => show(a.dataset.view)));

/* ------------------------------------------------------------------ overview */
let engineOK = false;
async function loadOverview() {
  try {
    const o = await api("/api/overview");
    const env = o.environment, d = o.engine;
    engineOK = !!d.available;
    $("#version").textContent = `v${o.version}`;
    $("#engine-pill").className = `pill ${d.available ? "pill-ok" : "pill-bad"}`;
    $("#engine-pill").textContent = d.available ? `${d.engine} ${d.server_version}${d.rootless ? " (rootless)" : ""}` : "Engine: not connected";
    $("#engine-banner").innerHTML = d.available ? "" :
      `<div class="banner"><b>Container engine API not reachable.</b> Network tools still work, but container discovery, management, topology and the matrix need the Podman socket. Run NetScope with <code>-v &lt;podman socket&gt;:/run/podman/podman.sock --security-opt label=disable</code> (the scripts in <code>scripts/</code> do this for you).<br><span class="small">${esc(d.error)}</span></div>`;

    const up = env.interfaces.filter((i) => i.is_up).length;
    $("#stat-cards").innerHTML = [
      ["Engine", d.available ? `${d.engine} ${d.server_version}` : "-", true],
      ["Containers running", d.available ? `${d.containers_running}/${d.containers_total}` : "-"],
      ["Interfaces up", `${up}/${env.interfaces.length}`],
      ["Routes", env.routes.length],
      ["Container DNS", { podman: "aardvark-dns", docker: "127.0.0.11" }[env.dns.container_dns] || "none", true],
      ["Host", env.hostname, true],
    ].map(([k, v, small]) => `<div class="card"><div class="k">${k}</div><div class="v ${small ? "small" : ""}">${esc(v)}</div></div>`).join("");

    $("#ns-host").textContent = env.in_container ? `(${env.in_container} container ${env.hostname})` : `(host ${env.hostname})`;
    $("#iface-table").innerHTML = table(["Interface", "State", "IPv4", "MAC", "MTU", "RX", "TX", "Err/Drop"],
      env.interfaces.map((i) => [
        `<span class="mono">${esc(i.name)}</span>`,
        i.is_up ? `<span class="pill pill-ok">up</span>` : `<span class="pill pill-muted">down</span>`,
        i.ipv4.map((a) => `<span class="chip">${esc(a)}</span>`).join(" "),
        `<span class="mono small">${esc(i.mac || "-")}</span>`, i.mtu,
        fmtBytes(i.rx_bytes), fmtBytes(i.tx_bytes),
        `<span class="mono small">${i.rx_errors + i.tx_errors}/${i.rx_dropped + i.tx_dropped}</span>`,
      ]));
    $("#route-table").innerHTML = table(["Destination", "Gateway", "Interface"],
      env.routes.map((r) => [`<span class="mono">${esc(r.destination)}</span>`, `<span class="mono">${esc(r.gateway || "-")}</span>`, esc(r.interface)]));
    $("#dns-conf").innerHTML = `<div class="kv">
        <div><div class="k">Nameservers</div><div class="v">${esc(env.dns.nameservers.join(", ") || "-")}</div></div>
        <div><div class="k">Search</div><div class="v">${esc(env.dns.search.join(" ") || "-")}</div></div>
        <div><div class="k">Options</div><div class="v">${esc(env.dns.options.join(" ") || "-")}</div></div></div>
      <p class="muted small">${esc(env.dns.container_dns_label)}${env.dns.container_dns ? ": resolves container names on user-defined networks." : ""}</p>`;
    $("#recent-runs").innerHTML = historyTable(o.recent);
  } catch (e) {
    $("#stat-cards").innerHTML = `<p class="err">Failed to load: ${esc(e.message)}</p>`;
  }
}
loaders.overview = loadOverview;

/* ------------------------------------------------------------------ containers */
let statsCache = {};
async function loadContainers(withStats = false) {
  const box = $("#containers-table");
  box.innerHTML = spin("Loading containers…");
  try {
    const all = $("#show-stopped").checked;
    const [cs, ns] = await Promise.all([api(`/api/containers?all=${all}`), api("/api/networks")]);
    if (withStats) {
      const st = await api("/api/stats");
      statsCache = Object.fromEntries(st.map((s) => [s.name, s]));
    }
    fillHosts(cs);
    box.innerHTML = table(["Name", "Status", "Image", "Networks / IP", "Ports", "Storage", "RX", "TX", "Manage"],
      cs.map((c) => {
        const s = statsCache[c.name] || {};
        const st = c.status === "running" ? "pill-ok" : (c.status === "exited" || c.status === "stopped") ? "pill-bad" : "pill-muted";
        const n = esc(c.name);
        const run = c.status === "running", paused = c.status === "paused";
        return [
          `<b class="mono">${n}</b>${c.is_self ? ` <span class="pill pill-muted">this app</span>` : ""}${c.compose_service ? `<div class="muted small">${esc(c.compose_project)}/${esc(c.compose_service)}</div>` : ""}`,
          `<span class="pill ${st}">${esc(c.status)}</span>${c.health ? `<div class="muted small">${esc(c.health)}</div>` : ""}`,
          `<span class="mono small">${esc(c.image)}</span>`,
          c.networks.length ? c.networks.map((n) => `<span class="chip">${esc(n.name)} <b>${esc(n.ip || "-")}</b></span>`).join("<br>")
                            : `<span class="muted small">${esc(c.network_mode)}</span>`,
          c.exposed_ports.map((p) => `<span class="chip">${p}</span>`).join("") +
            c.published_ports.map((p) => `<div class="muted small">${esc(p.host_ip)}:${esc(p.host_port)} → ${esc(p.container)}</div>`).join(""),
          (c.mounts || []).filter((m) => m.destination && !m.destination.endsWith(".sock")).map((m) =>
            `<div class="small"><span class="chip">${esc(m.type)}</span> <span class="mono">${esc((m.source || "").split(/[\\/]/).slice(-2).join("/"))} → ${esc(m.destination)}</span></div>`).join("") || `<span class="muted">-</span>`,
          s.rx_bytes != null ? fmtBytes(s.rx_bytes) : "-",
          s.tx_bytes != null ? fmtBytes(s.tx_bytes) : "-",
          `<div class="actions">
            <button class="btn tiny" onclick="showLogs('${n}')">logs</button>
            <button class="btn tiny" onclick="showInspect('${n}')">inspect</button>
            ${run ? `<button class="btn tiny" onclick="quickTool('scan','${n}')">scan</button>` : ""}
            ${c.is_self ? "" : run ? `<button class="btn tiny" onclick="manage('${n}','restart')">restart</button>
                                      <button class="btn tiny" onclick="manage('${n}','pause')">pause</button>
                                      <button class="btn tiny danger" onclick="manage('${n}','stop')">stop</button>`
                       : paused ? `<button class="btn tiny" onclick="manage('${n}','unpause')">unpause</button>`
                       : `<button class="btn tiny ok" onclick="manage('${n}','start')">start</button>`}
          </div>`,
        ];
      }));
    $("#networks-table").innerHTML = table(["Network", "Driver", "Internal", "Subnet", "Gateway", "Containers"],
      ns.map((n) => [
        `<b class="mono">${esc(n.name)}</b>`, esc(n.driver),
        n.internal ? `<span class="pill pill-warn">internal</span>` : "no",
        `<span class="mono">${esc(n.subnets.join(", ") || "-")}</span>`,
        `<span class="mono">${esc(n.gateways.join(", ") || "-")}</span>`,
        n.containers.map((m) => `<span class="chip">${esc(m.name)} <b>${esc(m.ipv4 || "")}</b></span>`).join(" ") || `<span class="muted">-</span>`,
      ]));
    if (withStats) toast("Traffic counters updated");
  } catch (e) {
    box.innerHTML = e.status === 503 ? engineHint(e.message) : `<p class="err">${esc(e.message)}</p>`;
    $("#networks-table").innerHTML = "";
  }
}
$("#show-stopped").addEventListener("change", () => loadContainers());

function detail(title, html) {
  $("#container-detail").innerHTML = `<div class="panel"><div class="view-head"><h2 style="margin:0">${title}</h2>
    <button class="btn ghost tiny" onclick="$('#container-detail').innerHTML=''">close</button></div>${html}</div>`;
  $("#container-detail").scrollIntoView({ behavior: "smooth" });
}
async function showLogs(name) {
  detail(`Logs · ${esc(name)}`, spin("Fetching logs…"));
  try {
    const r = await api(`/api/containers/${encodeURIComponent(name)}/logs?tail=200`);
    detail(`Logs · ${esc(name)} <span class="muted small">(podman logs --tail 200 ${esc(name)})</span>`,
      `<pre class="logs">${esc(r.logs || "(no output)")}</pre>`);
  } catch (e) { detail(`Logs · ${esc(name)}`, `<p class="err">${esc(e.message)}</p>`); }
}
async function showInspect(name) {
  detail(`Inspect · ${esc(name)}`, spin("Inspecting…"));
  try {
    const [r, top] = await Promise.all([
      api(`/api/containers/${encodeURIComponent(name)}/inspect`),
      api(`/api/containers/${encodeURIComponent(name)}/top`).catch(() => null)]);
    const st = r.State || {}, hc = r.HostConfig || {};
    const nets = Object.entries((r.NetworkSettings || {}).Networks || {});
    const fixRow = (p, n) => {  // Podman returns each process as one string: split into columns
      if (p.length >= n || n < 2) return p;
      const parts = String(p.join(" ")).trim().split(/\s+/);
      return parts.length > n ? [...parts.slice(0, n - 1), parts.slice(n - 1).join(" ")] : parts;
    };
    const procs = top && top.Processes ? table(top.Titles.map(esc), top.Processes.map((p) => fixRow(p, top.Titles.length).map((x) => `<span class="mono small">${esc(x)}</span>`))) : "";
    detail(`Inspect · ${esc(name)} <span class="muted small">(podman inspect ${esc(name)})</span>`,
      kv([["Status", st.Status], ["PID", st.Pid], ["Exit code", st.ExitCode], ["Started", (st.StartedAt || "").slice(0, 19)],
          ["Restart policy", (hc.RestartPolicy || {}).Name || "no"], ["Image", (r.Config || {}).Image],
          ["Networks", nets.map(([k, v]) => `${k}=${v.IPAddress}`).join(", ") || hc.NetworkMode],
          ["Mounts", (r.Mounts || []).length]])
      + (procs ? `<h2 class="mt">Processes (podman top)</h2>${procs}` : "")
      + `<details open><summary>Full inspect JSON</summary><pre>${esc(JSON.stringify(r, null, 2))}</pre></details>`);
  } catch (e) { detail(`Inspect · ${esc(name)}`, `<p class="err">${esc(e.message)}</p>`); }
}
async function manage(name, action) {
  if (action === "stop" && !confirm(`Stop container ${name}?`)) return;
  try {
    const r = await api(`/api/containers/${encodeURIComponent(name)}/${action}`, { method: "POST" });
    toast(r.summary);
  } catch (e) { toast(`Error: ${e.message}`); }
  loadContainers();
}
loaders.containers = () => loadContainers(true);

function engineHint(msg) {
  return `<div class="banner"><b>Podman API socket not available.</b> Start NetScope with the <code>scripts/podman-up</code> script (it mounts the Podman socket) to enable this view.<br><span class="small">${esc(msg)}</span></div>`;
}

function fillHosts(cs) {
  $("#hosts").innerHTML = cs.filter((c) => c.status === "running")
    .map((c) => `<option value="${esc(c.name)}">`).join("");
}

/* ------------------------------------------------------------------ topology */
async function loadTopology() {
  const box = $("#topology");
  box.innerHTML = spin("Mapping networks…");
  try {
    const t = await api("/api/topology");
    box.innerHTML = renderTopology(t);
  } catch (e) {
    box.innerHTML = e.status === 503 ? engineHint(e.message) : `<p class="err">${esc(e.message)}</p>`;
  }
}
loaders.topology = loadTopology;

function renderTopology(t) {
  if (!t.networks.length) return `<p class="muted">No running containers on any network.</p>`;
  const short = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
  const netIdx = Object.fromEntries(t.networks.map((n, i) => [n.name, i]));
  const ipsOf = {};
  t.edges.forEach((e) => (ipsOf[e.container] ||= []).push(e));
  // order containers by the average position of their networks -> fewer crossing lines
  const bary = (c) => {
    const idx = (ipsOf[c.name] || []).map((e) => netIdx[e.network]).filter((i) => i != null);
    return idx.length ? idx.reduce((a, b) => a + b, 0) / idx.length : 999;
  };
  const containers = [...t.containers].sort((a, b) => bary(a) - bary(b) || a.name.localeCompare(b.name));

  const netW = 200, nodeW = 168, gapN = 60, gapC = 18, pad = 20;
  const W = Math.max(t.networks.length * (netW + gapN), containers.length * (nodeW + gapC)) + pad * 2;
  const netX = (i) => (W - t.networks.length * (netW + gapN) + gapN) / 2 + i * (netW + gapN);
  const conX = (i) => (W - containers.length * (nodeW + gapC) + gapC) / 2 + i * (nodeW + gapC);
  const netPos = Object.fromEntries(t.networks.map((n, i) => [n.name, netX(i) + netW / 2]));
  const conPos = Object.fromEntries(containers.map((c, i) => [c.name, conX(i) + nodeW / 2]));
  const ny = 30, nh = 54, cy = 250;
  const nodeH = (c) => 48 + 14 * Math.max(1, (ipsOf[c.name] || []).length);
  const H = cy + Math.max(...containers.map(nodeH)) + 30;

  const edges = t.edges.filter((e) => netPos[e.network] != null && conPos[e.container] != null).map((e) => {
    const x1 = netPos[e.network], y1 = ny + nh, x2 = conPos[e.container], y2 = cy, my = (y1 + y2) / 2;
    return `<path class="topo-edge" d="M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}"><title>${esc(e.container)} on ${esc(e.network)}: ${esc(e.ip || "")}</title></path>`;
  }).join("");
  const nets = t.networks.map((n, i) => `
    <g class="topo-net ${n.internal ? "internal" : ""}" transform="translate(${netX(i)},${ny})">
      <rect width="${netW}" height="${nh}" rx="10"/>
      <text x="${netW / 2}" y="22" text-anchor="middle" font-weight="600">${esc(short(n.name, 24))}${n.internal ? " 🔒" : ""}</text>
      <text class="sub" x="${netW / 2}" y="40" text-anchor="middle">${esc(n.driver)} · ${esc(n.subnets[0] || "")}</text>
    </g>`).join("");
  const nodes = containers.map((c, i) => {
    const ips = (ipsOf[c.name] || []).map((e, k) =>
      `<text class="ip" x="${nodeW / 2}" y="${52 + k * 14}" text-anchor="middle">${esc(e.ip || "-")} <tspan class="sub">${esc(short(e.network.replace(/^.*?_/, ""), 12))}</tspan></text>`).join("");
    return `<g class="topo-node ${c.status !== "running" ? "down" : ""}" transform="translate(${conX(i)},${cy})">
      <rect width="${nodeW}" height="${nodeH(c)}" rx="8"/>
      <text x="${nodeW / 2}" y="20" text-anchor="middle" font-weight="600">${esc(short(c.name, 22))}</text>
      <text class="sub" x="${nodeW / 2}" y="35" text-anchor="middle">${esc(short(c.image || "", 24))}${c.ports.length ? " · :" + esc(c.ports.slice(0, 2).join(",:")) : ""}</text>
      ${ips || `<text class="sub" x="${nodeW / 2}" y="52" text-anchor="middle">${esc(c.network_mode || "")}</text>`}
    </g>`;
  }).join("");
  return `<svg class="topo" viewBox="0 0 ${W} ${H}" width="100%" style="min-width:${Math.round(W * 0.75)}px">${edges}${nets}${nodes}</svg>`;
}

/* ------------------------------------------------------------------ tools */
const TOOLS = {
  ping: { fields: [["host", "Host / container", "demo-web", true], ["count", "Count", 4]], url: "/api/tools/ping" },
  latency: { fields: [["host", "Host / container", "demo-cache", true], ["port", "Port", 6379], ["count", "Samples", 10]], url: "/api/tools/latency" },
  dns: { fields: [["name", "Name", "demo-db", true], ["record_type", "Type", "A", false, ["A", "AAAA", "CNAME", "MX", "NS", "TXT", "SRV", "PTR"]], ["server", "DNS server (optional)", ""]], url: "/api/tools/dns" },
  scan: { fields: [["host", "Host / container", "demo-api", true], ["ports", "Ports", "common"], ["timeout", "Timeout (s)", 1]], url: "/api/tools/scan" },
  http: { fields: [["url", "URL", "http://demo-web", true], ["method", "Method", "GET", false, ["GET", "HEAD"]]], url: "/api/tools/http" },
  traceroute: { fields: [["host", "Host", "8.8.8.8", true], ["max_hops", "Max hops", 15]], url: "/api/tools/traceroute" },
};
let tool = "ping";

function renderFields() {
  $("#tool-fields").innerHTML = TOOLS[tool].fields.map(([name, label, def, grow, options]) => `
    <div class="field ${grow ? "grow" : ""}">
      <label for="f-${name}">${label}</label>
      ${options ? `<select id="f-${name}" name="${name}">${options.map((o) => `<option ${o === def ? "selected" : ""}>${o}</option>`).join("")}</select>`
                : `<input id="f-${name}" name="${name}" value="${esc(def)}" ${grow ? 'list="hosts"' : ""} ${typeof def === "number" ? 'type="number" step="any"' : ""}>`}
    </div>`).join("");
}
document.querySelectorAll("#tool-tabs button").forEach((b) => b.addEventListener("click", () => {
  tool = b.dataset.tool;
  document.querySelectorAll("#tool-tabs button").forEach((x) => x.classList.toggle("active", x === b));
  renderFields();
}));

function quickTool(name, host) {
  show("tools");
  document.querySelector(`#tool-tabs button[data-tool="${name}"]`).click();
  $("#tool-form").querySelector("input").value = host;
  $("#tool-form").requestSubmit();
}

$("#tool-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = {};
  for (const [name, , def] of TOOLS[tool].fields) {
    const v = $(`#f-${name}`).value.trim();
    if (v === "") continue;
    body[name] = typeof def === "number" ? Number(v) : v;
  }
  const btn = $("#run-btn"); btn.disabled = true;
  $("#tool-result").innerHTML = `<div class="panel">${spin()}</div>`;
  try {
    const r = await api(TOOLS[tool].url, { method: "POST", body });
    $("#tool-result").innerHTML = renderResult(r);
  } catch (e) {
    $("#tool-result").innerHTML = `<div class="panel"><p class="err">${esc(e.message)}</p></div>`;
  } finally { btn.disabled = false; }
});
loaders.tools = async () => { try { fillHosts(await api("/api/containers?all=false")); } catch { /* no engine */ } };

const kv = (pairs) => `<div class="kv">${pairs.filter(([, v]) => v !== undefined).map(([k, v]) =>
  `<div><div class="k">${k}</div><div class="v">${esc(v ?? "-")}</div></div>`).join("")}</div>`;
const ms = (v) => (v == null ? "-" : `${v} ms`);
const bars = (vals) => {
  if (!vals?.length) return "";
  const max = Math.max(...vals) || 1;
  return `<div class="bars">${vals.map((v) => `<span title="${v} ms" style="height:${Math.max(3, (v / max) * 100)}%"></span>`).join("")}</div>`;
};

function renderResult(r) {
  let body = "";
  switch (r.tool) {
    case "ping":
      body = kv([["Resolved IP", r.resolved_ip], ["Method", r.method], ["Sent/Recv", `${r.sent ?? "-"}/${r.received ?? "-"}`],
                 ["Loss", r.loss_pct != null ? r.loss_pct + "%" : "-"], ["Min", ms(r.min_ms)], ["Avg", ms(r.avg_ms)], ["Max", ms(r.max_ms)], ["Jitter", ms(r.jitter_ms)]])
        + bars((r.replies || []).map((x) => x.time_ms) .concat(r.samples_ms || []));
      break;
    case "latency":
      body = kv([["Target", r.target], ["Success", `${r.received}/${r.sent}`], ["Loss", r.loss_pct + "%"], ["Min", ms(r.min_ms)],
                 ["Avg", ms(r.avg_ms)], ["p95", ms(r.p95_ms)], ["Max", ms(r.max_ms)], ["Jitter", ms(r.jitter_ms)]]) + bars(r.samples_ms);
      break;
    case "dns":
      body = kv([["Type", r.record_type], ["Query time", r.query_ms + " ms"], ["TTL", r.ttl], ["Nameservers", (r.nameservers || []).join(", ")],
                 ["System resolver", r.system_resolver?.ip || r.system_resolver?.error]])
        + (r.answers?.length ? table(["Answer"], r.answers.map((a) => [`<span class="mono">${esc(a)}</span>`])) : "");
      break;
    case "scan":
      body = kv([["Resolved IP", r.resolved_ip], ["Scanned", r.ports_scanned], ["Open", r.counts?.open], ["Closed", r.counts?.closed],
                 ["Filtered", r.counts?.filtered], ["Duration", r.duration_s != null ? r.duration_s + " s" : undefined]])
        + table(["Port", "State", "Service", "Connect time"], (r.open || []).map((p) =>
          [`<b class="mono">${p.port}</b>`, `<span class="pill pill-ok">open</span>`, esc(p.service || "-"), `${p.latency_ms} ms`]));
      break;
    case "http":
      body = kv([["Status", r.status_code ? `${r.status_code} ${r.reason}` : undefined], ["HTTP", r.http_version], ["Resolved IP", r.resolved_ip],
                 ["DNS", r.timings?.dns_ms + " ms"], ["Total", r.timings?.total_ms != null ? r.timings.total_ms + " ms" : undefined],
                 ["Body", r.body_bytes != null ? fmtBytes(r.body_bytes) : undefined]])
        + (r.redirects?.length ? `<p class="muted small">Redirects: ${r.redirects.map(esc).join(" → ")} → ${esc(r.final_url)}</p>` : "")
        + (r.headers ? table(["Header", "Value"], Object.entries(r.headers).map(([k, v]) => [`<span class="mono">${esc(k)}</span>`, `<span class="mono small">${esc(v)}</span>`])) : "")
        + (r.tls ? `<h2 class="mt">TLS</h2>` + kv(Object.entries(r.tls).map(([k, v]) => [k, String(v)])) : "");
      break;
    case "traceroute":
      body = kv([["Resolved IP", r.resolved_ip], ["Hops", r.hops?.length], ["Reached", r.reached ? "yes" : "no"]])
        + table(["Hop", "IP", "RTT"], (r.hops || []).map((h) => [h.hop, `<span class="mono">${esc(h.ip || "*")}</span>`, h.rtt_ms != null ? h.rtt_ms + " ms" : "-"]));
      break;
  }
  return `<div class="panel">
    <div class="result-head">${pill(r.ok)}<span class="summary">${esc(r.summary)}</span></div>
    ${r.error ? `<p class="err">${esc(r.error)}</p>` : ""}${r.note ? `<p class="muted small">${esc(r.note)}</p>` : ""}
    ${body}
    <details><summary>Raw JSON</summary><pre>${esc(JSON.stringify(r, null, 2))}</pre></details>
  </div>`;
}

/* ------------------------------------------------------------------ matrix */
async function runMatrix() {
  const btn = $("#matrix-btn"); btn.disabled = true;
  $("#matrix-out").innerHTML = spin("Launching probe sidecars in each container's network namespace… (can take ~10-30 s)");
  $("#matrix-findings").innerHTML = "";
  try {
    const m = await api("/api/matrix", { method: "POST" });
    const targets = Object.values(m.targets).flat();
    if (!targets.length) {
      $("#matrix-out").innerHTML = `<p class="muted">No running containers expose TCP ports, so there is nothing to probe.</p>`;
      return;
    }
    const rows = m.rows.map((row) => {
      const byT = Object.fromEntries(row.cells.map((c) => [c.target, c]));
      const cells = targets.map((t) => {
        if (t.startsWith(row.source + ":")) return `<td class="cell self"></td>`;
        const c = byT[t];
        if (!c) return `<td class="cell m-iso" title="${esc(row.error || "")}">${row.error ? "err" : ""}</td>`;
        const cls = c.status === "ok" ? (c.expected ? "m-ok" : "m-warn") : (c.expected ? "m-bad" : "m-iso");
        const label = c.status === "ok" ? `${c.latency_ms}ms` : c.status.replace("_", " ");
        const tip = `${row.source} → ${t}: ${c.status}${c.shared_networks.length ? " | shared: " + c.shared_networks.join(", ") : " | no shared network"}${c.detail ? " | " + c.detail : ""}`;
        return `<td class="cell ${cls}" title="${esc(tip)}">${esc(label)}</td>`;
      }).join("");
      return `<tr><td class="src">${esc(row.source)}${row.error ? ` <span class="pill pill-bad" title="${esc(row.error)}">probe error</span>` : ""}</td>${cells}</tr>`;
    }).join("");
    $("#matrix-out").innerHTML = `<p class="muted small">${esc(m.summary)} · probe image <span class="mono">${esc(m.probe_image)}</span> · rows = source, columns = target. Hover a cell for details.</p>
      <table class="matrix"><thead><tr><th>source ↓ / target →</th>${targets.map((t) => `<th class="rot">${esc(t)}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table>`;
    const errs = m.rows.filter((r) => r.error).map((r) => `<div class="finding warning"><b>${esc(r.source)}</b>: ${esc(r.error)}</div>`).join("");
    $("#matrix-findings").innerHTML = (m.findings.length || errs) ? `<div class="panel"><h2>Findings</h2>${errs}${m.findings.map((f) =>
      `<div class="finding ${f.severity}">${esc(f.message)}</div>`).join("")}</div>`
      : `<div class="panel"><p>${pill(true, "CLEAN")} Every path behaves exactly as the network design predicts.</p></div>`;
  } catch (e) {
    $("#matrix-out").innerHTML = e.status === 503 ? engineHint(e.message) : `<p class="err">${esc(e.message)}</p>`;
  } finally { btn.disabled = false; }
}

/* ------------------------------------------------------------------ history */
function historyTable(runs) {
  return table(["#", "Time (UTC)", "Tool", "Result", "Summary"], runs.map((r) => [
    r.id, `<span class="mono small">${esc((r.ts || "").replace("T", " ").replace("+00:00", ""))}</span>`,
    `<span class="chip">${esc(r.tool)}</span>`, pill(r.ok), `<span class="mono small">${esc(r.summary)}</span>`]),
    (i) => `class="click" onclick="openRun(${runs[i].id})"`);
}
async function loadHistory() {
  $("#history-table").innerHTML = spin("Loading…");
  try { $("#history-table").innerHTML = historyTable(await api("/api/history?limit=200")); }
  catch (e) { $("#history-table").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}
loaders.history = loadHistory;

async function openRun(id) {
  if (current !== "history") show("history");
  const r = await api(`/api/history/${id}`);
  $("#history-detail").innerHTML = r.tool === "matrix"
    ? `<div class="panel"><h2>Run #${id} · matrix</h2><pre>${esc(JSON.stringify(r, null, 2))}</pre></div>`
    : renderResult(r);
  $("#history-detail").scrollIntoView({ behavior: "smooth" });
}
async function saveReport() {
  try {
    const r = await api("/api/report/save", { method: "POST" });
    toast(`Saved ${r.file} to the bind-mounted reports folder`);
    $("#history-detail").innerHTML = `<div class="panel">${pill(true, "SAVED")} <span class="mono">${esc(r.saved)}</span>
      <p class="muted small">/reports is a bind mount, so this file is now in the <b>reports</b> folder of the project on your computer (${r.reports_in_folder} reports there).</p></div>`;
  } catch (e) { toast(`Error: ${e.message}`); }
}
async function clearHistory() {
  if (!confirm("Delete all stored diagnostic runs?")) return;
  const r = await api("/api/history", { method: "DELETE" });
  toast(`Deleted ${r.deleted} runs`); $("#history-detail").innerHTML = ""; loadHistory();
}

/* ------------------------------------------------------------------ boot */
renderFields();
loadOverview();
const start = location.hash.slice(1);
if (start && loaders[start] !== undefined && start !== "overview") show(start);
