/**
 * Infrastructure Monitor add-on UI.
 *
 * Served through Home Assistant Ingress, so every request URL is relative:
 * the Supervisor mounts this page under a per-session path prefix and absolute
 * paths would escape it.
 */

const TYPE_LABEL = {
  mikrotik: "MikroTik",
  swos: "MikroTik SwOS",
  proxmox: "Proxmox VE",
  redfish: "Redfish BMC",
};

const REFRESH_MS = 10000;

// view is either {name: "list"} or {name: "detail", sourceId}
// view is {name: "list"}, {name: "detail", sourceId} or {name: "events"}
const state = {
  sources: [],
  token: "",
  view: { name: "list" },
  detail: null,
  filter: "",
  events: [],
  eventFilter: "",
};

// How each event kind reads: the same tones the monitor rows use.
const EVENT_TONE = {
  problem: "bad",
  threshold_critical: "bad",
  source_unavailable: "bad",
  threshold_warning: "warn",
  update_available: "warn",
  recovery: "good",
  threshold_clear: "good",
  source_available: "good",
  state_change: "",
};

const EVENT_LABEL = {
  problem: "Problem",
  recovery: "Recovered",
  threshold_warning: "Warning threshold",
  threshold_critical: "Critical threshold",
  threshold_clear: "Threshold cleared",
  state_change: "State change",
  update_available: "Update available",
  source_available: "Device back",
  source_unavailable: "Device unreachable",
};

// -- transport ----------------------------------------------------------

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.error || `HTTP ${response.status}`);
  }
  return body;
}

function banner(message, kind = "error") {
  const node = document.getElementById("banner");
  if (!message) {
    node.classList.add("hidden");
    return;
  }
  node.textContent = message;
  node.className = `banner ${kind}`;
}

// -- rendering ----------------------------------------------------------

function relativeTime(iso) {
  if (!iso) return "never";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso)) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

/** Whether an update line has something newer waiting to be installed. */
function isPending(update) {
  return Boolean(
    update.latest_version && update.installed_version !== update.latest_version
  );
}

/**
 * Badge naming the updates a device is waiting on.
 *
 * Empty when there is nothing pending: a device that is up to date should not
 * carry a "0 updates" ornament on every card.
 */
function updateBadge(names) {
  const pending = names || [];
  if (!pending.length) return "";
  const label = pending.length === 1 ? "1 update" : `${pending.length} updates`;
  return `<span class="badge update" title="${escapeHtml(
    pending.join(", ")
  )}">${label} pending</span>`;
}

function renderSummary() {
  const total = state.sources.length;
  const ok = state.sources.filter((s) => s.status.available).length;
  const broken = state.sources.filter(
    (s) => s.enabled && !s.status.available
  ).length;
  const entities = state.sources.reduce((n, s) => n + s.status.entities, 0);
  const updates = state.sources.reduce(
    (n, s) => n + (s.status.pending_updates || []).length,
    0
  );

  document.getElementById("summary").innerHTML = `
    ${statTile(total, "Devices")}
    ${statTile(ok, "Reporting", ok === total ? "good" : "")}
    ${statTile(broken, "Failing", broken ? "bad" : "good")}
    ${statTile(updates, "Updates pending", updates ? "warn" : "good")}
    ${statTile(entities, "Entities")}
  `;
}

function statTile(value, label, tone = "") {
  return `<div class="stat ${tone}">
    <div class="stat-value">${value}</div>
    <div class="stat-label">${label}</div>
  </div>`;
}

function escapeHtml(text) {
  return String(text ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );
}

function renderSources() {
  const container = document.getElementById("sources");
  if (!state.sources.length) {
    container.innerHTML = `<div class="empty">
      <p>No devices configured yet.</p>
      <button class="btn primary" onclick="window.__openEditor()">Add your first device</button>
    </div>`;
    return;
  }

  container.innerHTML = state.sources
    .map((source) => {
      const status = source.status;
      let tone = "bad";
      // The error text names the endpoint that failed, which is what makes a
      // permissions problem findable, so it is not replaced with a generic
      // "Authentication failed".
      let text = status.error || "Not started";
      if (!source.enabled) {
        tone = "off";
        text = "Disabled";
      } else if (status.available) {
        tone = "good";
        text = "Reporting";
      }

      // A second host in the same Proxmox cluster sees exactly the same data,
      // so only one of them publishes it to Home Assistant.
      const cluster = status.cluster;
      if (cluster && !cluster.reporting && status.available) {
        tone = "off";
        text = `Standby — reported by ${cluster.reported_by}`;
      }

      return `<article class="card">
        <div class="card-head">
          <div>
            <div class="card-title">${escapeHtml(source.name)}</div>
            <div class="card-meta">
              <span class="badge">${TYPE_LABEL[source.type] || source.type}</span>
              <code>${escapeHtml(source.host)}:${source.port}</code>
              ${
                cluster
                  ? `<span class="tag">cluster of ${cluster.members}</span>`
                  : ""
              }
              ${updateBadge(status.pending_updates)}
            </div>
          </div>
          <div class="card-status" title="${escapeHtml(text)}">
            <span class="dot ${tone}"></span>
            <span class="status-text">${escapeHtml(text)}</span>
          </div>
        </div>
        <div class="card-stats">
          <div><strong>${status.entities}</strong><span>entities</span></div>
          <div><strong>${relativeTime(status.last_update)}</strong><span>last poll</span></div>
          <div><strong>${source.scan_interval}s</strong><span>interval</span></div>
        </div>
        <div class="card-actions">
          <button class="btn primary" data-action="view" data-id="${source.id}">View monitors</button>
          <button class="btn" data-action="refresh" data-id="${source.id}">Poll now</button>
          <button class="btn" data-action="edit" data-id="${source.id}">Edit</button>
          <button class="btn danger" data-action="delete" data-id="${source.id}">Remove</button>
        </div>
      </article>`;
    })
    .join("");
}

// -- detail view --------------------------------------------------------

/** Format a sensor reading for display, honouring unit and precision. */
function formatValue(reading) {
  const value = reading.value;
  if (value === null || value === undefined || value === "") return "—";
  if (reading.device_class === "timestamp") {
    const when = new Date(value);
    return Number.isNaN(when.getTime())
      ? String(value)
      : `${when.toLocaleString()} (${relativeTime(value)})`;
  }
  let text = value;
  if (typeof value === "number") {
    const precision = reading.suggested_display_precision;
    text =
      precision === null || precision === undefined
        ? String(value)
        : value.toFixed(precision);
  }
  return reading.unit ? `${text} ${reading.unit}` : String(text);
}

/** Turn a binary reading into label plus tone, using its device class. */
function formatBinary(reading) {
  if (reading.value === null || reading.value === undefined) {
    return { text: "Unknown", tone: "" };
  }
  switch (reading.device_class) {
    case "problem":
      return reading.value
        ? { text: "Problem", tone: "bad" }
        : { text: "OK", tone: "good" };
    case "connectivity":
      return reading.value
        ? { text: "Connected", tone: "good" }
        : { text: "Disconnected", tone: "bad" };
    case "running":
      return reading.value
        ? { text: "Running", tone: "good" }
        : { text: "Stopped", tone: "" };
    case "update":
      return reading.value
        ? { text: "Update available", tone: "warn" }
        : { text: "Up to date", tone: "good" };
    default:
      return reading.value ? { text: "On", tone: "" } : { text: "Off", tone: "" };
  }
}

/** Order devices so children follow their parent, roots first. */
function deviceTree(devices) {
  const byKey = new Map(devices.map((d) => [d.key, d]));
  const children = new Map();
  const roots = [];
  devices.forEach((device) => {
    const parent = device.via_device;
    if (parent && byKey.has(parent)) {
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(device);
    } else {
      roots.push(device);
    }
  });

  const ordered = [];
  const walk = (device, depth) => {
    ordered.push({ device, depth });
    (children.get(device.key) || [])
      .sort((a, b) => a.name.localeCompare(b.name))
      .forEach((child) => walk(child, depth + 1));
  };
  roots.forEach((root) => walk(root, 0));
  return ordered;
}

function matchesFilter(text) {
  return !state.filter || String(text).toLowerCase().includes(state.filter);
}

/** Per-line settings for a monitor key, or the empty defaults. */
function overrideFor(source, key) {
  const stored = (source.overrides || {})[key] || {};
  return { muted: Boolean(stored.muted), thresholds: stored.thresholds || {} };
}

/**
 * Opening `<tr>` for a clickable monitor line, plus the badges that show at a
 * glance why it is muted or unhappy without opening the modal.
 */
function monitorRow(source, item, kind) {
  const override = overrideFor(source, item.key);
  const classes = ["monitor-row"];
  if (!item.enabled_default) classes.push("off-by-default");
  if (override.muted) classes.push("muted");
  if (item.severity) classes.push(`sev-${item.severity}`);

  const badges = [
    item.enabled_default ? "" : '<span class="tag">hidden in HA</span>',
    override.muted ? '<span class="tag muted-tag">muted</span>' : "",
    Object.keys(override.thresholds).length
      ? '<span class="tag">threshold</span>'
      : "",
  ]
    .filter(Boolean)
    .join(" ");

  return `<tr class="${classes.join(" ")}" data-monitor="${escapeHtml(item.key)}"
      data-kind="${kind}" tabindex="0" title="${escapeHtml(
        item.reason || "Click to mute or set thresholds"
      )}">
    <td class="k">${escapeHtml(item.name)}${badges ? ` ${badges}` : ""}${
      item.reason ? `<span class="reason">${escapeHtml(item.reason)}</span>` : ""
    }</td>`;
}

function renderReadingRows(source, deviceKey) {
  const rows = [];

  source.sensors
    .filter((s) => s.device_key === deviceKey && matchesFilter(s.name))
    .forEach((sensor) => {
      rows.push(`${monitorRow(source, sensor, "sensor")}
        <td class="v">${escapeHtml(formatValue(sensor))}</td>
      </tr>`);
    });

  source.binary_sensors
    .filter((b) => b.device_key === deviceKey && matchesFilter(b.name))
    .forEach((binary) => {
      const { text, tone } = formatBinary(binary);
      rows.push(`${monitorRow(source, binary, "binary_sensor")}
        <td class="v"><span class="chip ${tone}">${escapeHtml(text)}</span></td>
      </tr>`);
    });

  source.updates
    .filter((u) => u.device_key === deviceKey && matchesFilter(u.name))
    .forEach((update) => {
      const pending = isPending(update);
      const install = update.can_install
        ? `<button class="btn small" data-action="install" data-id="${source.id}"
             data-key="${escapeHtml(update.key)}">Install</button>`
        : '<span class="tag">read-only</span>';
      rows.push(`${monitorRow(source, update, "update")}
        <td class="v">${
          pending
            ? `<span class="chip warn">${escapeHtml(update.installed_version)} → ${escapeHtml(
                update.latest_version
              )}</span> ${install}`
            : `<span class="chip good">${escapeHtml(update.installed_version || "—")}</span>`
        }</td>
      </tr>`);
    });

  source.switches
    .filter((s) => s.device_key === deviceKey && matchesFilter(s.name))
    .forEach((entry) => {
      rows.push(`<tr>
        <td class="k">${escapeHtml(entry.name)} <span class="tag">control</span></td>
        <td class="v"><span class="chip ${entry.value ? "good" : ""}">${
          entry.value === null || entry.value === undefined
            ? "Unknown"
            : entry.value
              ? "On"
              : "Off"
        }</span></td>
      </tr>`);
    });

  const buttons = source.buttons.filter(
    (b) => b.device_key === deviceKey && matchesFilter(b.name)
  );
  if (buttons.length) {
    rows.push(`<tr>
      <td class="k">Actions</td>
      <td class="v">${buttons
        .map(
          (b) =>
            `<button class="btn small" data-action="press" data-id="${source.id}"
               data-key="${escapeHtml(b.key)}">${escapeHtml(b.name)}</button>`
        )
        .join(" ")}</td>
    </tr>`);
  }

  return rows;
}

function renderDetail() {
  const container = document.getElementById("detail-view");
  // The 10s auto-refresh re-renders this view, which would otherwise yank the
  // caret out of the filter box mid-typing.
  const active = document.activeElement;
  const hadFocus = Boolean(active && active.id === "detail-filter");
  const caret = hadFocus ? active.selectionStart : 0;
  const source = state.detail;
  if (!source) {
    container.innerHTML = `<div class="banner">Loading…</div>`;
    return;
  }

  const total =
    source.sensors.length +
    source.binary_sensors.length +
    source.switches.length +
    source.updates.length;

  const pendingUpdates = source.updates.filter(isPending).map((u) => u.name);

  const devices = deviceTree(source.devices)
    .map(({ device, depth }) => {
      const rows = renderReadingRows(source, device.key);
      if (!rows.length) return "";
      const meta = [device.model, device.sw_version && `v${device.sw_version}`]
        .filter(Boolean)
        .join(" · ");
      return `<section class="device${depth ? " nested" : ""}" style="--depth:${depth}">
        <header class="device-head">
          <h3>${escapeHtml(device.name)}</h3>
          ${meta ? `<span class="device-meta">${escapeHtml(meta)}</span>` : ""}
        </header>
        <table class="readings"><tbody>${rows.join("")}</tbody></table>
      </section>`;
    })
    .join("");

  container.innerHTML = `
    <div class="detail-head">
      <button class="btn" id="back-to-list">← All devices</button>
      <div class="detail-title">
        <h2>${escapeHtml(source.name)}</h2>
        <div class="card-meta">
          <span class="badge">${TYPE_LABEL[source.type] || source.type}</span>
          <code>${escapeHtml(source.host)}:${source.port}</code>
          <span class="device-meta">${total} monitors · polled ${relativeTime(
            source.last_update
          )}</span>
          ${updateBadge(pendingUpdates)}
        </div>
      </div>
      <button class="btn" data-action="refresh" data-id="${source.id}">Poll now</button>
    </div>
    ${
      source.error
        ? `<div class="banner error">${escapeHtml(source.error)}</div>`
        : ""
    }
    <input id="detail-filter" class="filter" type="search"
           placeholder="Filter monitors…" value="${escapeHtml(state.filter)}" />
    ${
      devices ||
      `<div class="empty"><p>${
        state.filter
          ? "Nothing matches that filter."
          : "No readings yet — the device has not been polled successfully."
      }</p></div>`
    }
  `;

  document
    .getElementById("back-to-list")
    .addEventListener("click", () => showList());

  const filter = document.getElementById("detail-filter");
  filter.addEventListener("input", (event) => {
    state.filter = event.target.value.trim().toLowerCase();
    renderDetail();
  });
  if (hadFocus) {
    filter.focus();
    filter.setSelectionRange(caret, caret);
  }
}

// -- event log ----------------------------------------------------------

function renderEvents() {
  const container = document.getElementById("events-view");
  const filter = state.eventFilter;
  // Newest first: a log is read from the top.
  const events = state.events
    .filter(
      (e) =>
        !filter ||
        [e.name, e.source_name, e.device_name, e.kind, e.reason]
          .filter(Boolean)
          .some((text) => String(text).toLowerCase().includes(filter))
    )
    .slice()
    .reverse();

  const rows = events
    .map((event) => {
      const tone = EVENT_TONE[event.kind] ?? "";
      const label = EVENT_LABEL[event.kind] || event.kind;
      const where = [event.source_name, event.device_name]
        .filter(Boolean)
        // The device name repeats the source name on single-device sources.
        .filter((value, index, all) => all.indexOf(value) === index)
        .join(" › ");
      const change =
        event.old_state !== null &&
        event.old_state !== undefined &&
        event.new_state !== null &&
        event.new_state !== undefined
          ? `${formatState(event.old_state)} → ${formatState(event.new_state)}`
          : "";
      return `<tr>
        <td class="ev-when" title="${escapeHtml(event.timestamp || "")}">
          ${escapeHtml(relativeTime(event.timestamp))}
        </td>
        <td class="ev-kind"><span class="chip ${tone}">${escapeHtml(label)}</span></td>
        <td class="ev-what">
          <strong>${escapeHtml(event.name || event.entity_key || "")}</strong>
          <span class="ev-where">${escapeHtml(where)}</span>
        </td>
        <td class="ev-why">${escapeHtml(event.reason || change || "")}</td>
      </tr>`;
    })
    .join("");

  container.innerHTML = `
    <div class="detail-head">
      <button class="btn" id="events-back">← All devices</button>
      <div class="detail-title">
        <h2>Event log</h2>
        <div class="card-meta">
          <span class="device-meta">${state.events.length} recorded${
            filter ? ` · ${events.length} matching` : ""
          }</span>
        </div>
      </div>
    </div>
    <p class="hint">
      Every change the add-on detects. These are the same events the
      integration puts on the Home Assistant bus as
      <code>monitorha_event</code>, so anything listed here can trigger an
      automation. Holds the most recent 500; cleared when the add-on restarts.
    </p>
    <input id="events-filter" class="filter" type="search"
           placeholder="Filter events…" value="${escapeHtml(state.eventFilter)}" />
    ${
      rows
        ? `<table class="readings events-table"><tbody>${rows}</tbody></table>`
        : `<div class="empty"><p>${
            state.eventFilter
              ? "Nothing matches that filter."
              : "No events yet. Changes appear here as they are detected — the first poll of a device records nothing, since there is nothing yet to compare against."
          }</p></div>`
    }
  `;

  document.getElementById("events-back").addEventListener("click", showList);
  const box = document.getElementById("events-filter");
  const active = document.activeElement;
  box.addEventListener("input", (event) => {
    state.eventFilter = event.target.value.trim().toLowerCase();
    renderEvents();
  });
  // The 10s refresh re-renders this view, which would otherwise drop the caret.
  if (active && active.id === "events-filter") {
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);
  }
}

/** Render an event's old/new value compactly. */
function formatState(value) {
  if (typeof value === "boolean") return value ? "yes" : "no";
  return String(value);
}

async function loadEvents() {
  try {
    // since=0 returns the whole retained buffer, which is what a log wants.
    const data = await api("api/events?since=0");
    state.events = data.events || [];
    banner("");
  } catch (err) {
    banner(`Could not load events: ${err.message}`);
  }
  renderEvents();
}

async function showEvents() {
  state.view = { name: "events" };
  state.eventFilter = "";
  document.getElementById("list-view").classList.add("hidden");
  document.getElementById("detail-view").classList.add("hidden");
  document.querySelector(".token-panel").classList.add("hidden");
  document.getElementById("events-view").classList.remove("hidden");
  await loadEvents();
}

function showList() {
  state.view = { name: "list" };
  state.detail = null;
  state.filter = "";
  document.getElementById("list-view").classList.remove("hidden");
  document.getElementById("detail-view").classList.add("hidden");
  document.getElementById("events-view").classList.add("hidden");
  document.querySelector(".token-panel").classList.remove("hidden");
  load();
}

async function showDetail(sourceId) {
  state.view = { name: "detail", sourceId };
  state.filter = "";
  state.detail = null;
  document.getElementById("events-view").classList.add("hidden");
  document.getElementById("list-view").classList.add("hidden");
  document.querySelector(".token-panel").classList.add("hidden");
  document.getElementById("detail-view").classList.remove("hidden");
  renderDetail();
  await loadDetail();
}

async function loadDetail() {
  try {
    state.detail = await api(`api/sources/${state.view.sourceId}/snapshot`);
    banner("");
  } catch (err) {
    banner(`Could not load monitors: ${err.message}`);
  }
  renderDetail();
}

// -- loading ------------------------------------------------------------

async function load() {
  if (state.view.name === "detail") {
    await loadDetail();
    return;
  }
  if (state.view.name === "events") {
    await loadEvents();
    return;
  }
  try {
    const data = await api("api/sources");
    state.sources = data.sources;
    state.token = data.api_token;
    document.getElementById("api-token").textContent = data.api_token;
    banner("");
  } catch (err) {
    banner(`Could not load configuration: ${err.message}`);
  }
  renderSummary();
  renderSources();
}

// -- editor -------------------------------------------------------------

const editor = document.getElementById("editor");
const form = document.getElementById("editor-form");

const DEFAULTS = {
  mikrotik: { port: 443, scan_interval: 60, use_ssl: true, monitor_interfaces: true, monitor_tunnels: true, monitor_netwatch: true },
  swos: { port: 80, scan_interval: 60, monitor_ports: true, monitor_poe: true, monitor_sfp: true, check_firmware: true },
  proxmox: { port: 8006, scan_interval: 60, scope: "node", node: "", monitor_guests: true, monitor_backups: true, monitor_interfaces: true },
  redfish: { port: 443, scan_interval: 120, power_off_action: "graceful" },
};

function applyTypeVisibility() {
  const type = form.elements.type.value;
  const auth = form.elements.auth_method.value;
  const show = (selector, visible) =>
    editor
      .querySelectorAll(selector)
      .forEach((node) => node.classList.toggle("hidden", !visible));

  show(".only-mikrotik", type === "mikrotik");
  show(".only-swos", type === "swos");
  show(".only-proxmox", type === "proxmox");
  show(".only-redfish", type === "redfish");
  // MikroTik and Redfish always use username/password; Proxmox only does when
  // the token option is not selected.
  show(".only-token", type === "proxmox" && auth === "token");
  show(".only-userpass", type !== "proxmox" || auth === "password");
  // Naming a node only means anything when monitoring a single one.
  show(
    ".only-node-scope",
    type === "proxmox" && form.elements.scope.value === "node"
  );
}

function openEditor(source) {
  form.reset();
  document.getElementById("editor-message").classList.add("hidden");
  document.getElementById("editor-title").textContent = source
    ? "Edit device"
    : "Add device";

  const type = source?.type || "mikrotik";
  const values = { ...DEFAULTS[type], enabled: true, ...(source || {}) };

  form.elements.id.value = source?.id || "";
  form.elements.type.value = type;
  form.elements.auth_method.value = values.auth_method || "token";
  form.elements.scope.value = values.scope || "node";

  ["name", "host", "port", "token_id", "username", "node", "scan_interval", "slow_scan_interval"].forEach(
    (name) => {
      if (form.elements[name]) form.elements[name].value = values[name] ?? "";
    }
  );
  if (form.elements.power_off_action) {
    form.elements.power_off_action.value = values.power_off_action || "graceful";
  }
  // Secrets are never sent back to the browser; blank means "keep stored".
  form.elements.password.value = "";
  form.elements.token_secret.value = "";
  form.elements.password.placeholder = source ? "leave blank to keep" : "";
  form.elements.token_secret.placeholder = source ? "leave blank to keep" : "";

  ["verify_ssl", "use_ssl", "monitor_interfaces", "monitor_tunnels", "monitor_netwatch", "monitor_guests", "monitor_backups", "monitor_ports", "monitor_poe", "monitor_sfp", "check_firmware", "enabled"].forEach(
    (name) => {
      if (form.elements[name]) form.elements[name].checked = Boolean(values[name]);
    }
  );

  applyTypeVisibility();
  editor.classList.remove("hidden");
}

window.__openEditor = () => openEditor(null);

function collect() {
  const data = {};
  new FormData(form).forEach((value, key) => {
    data[key] = value;
  });
  ["verify_ssl", "use_ssl", "monitor_interfaces", "monitor_tunnels", "monitor_netwatch", "monitor_guests", "monitor_backups", "monitor_ports", "monitor_poe", "monitor_sfp", "check_firmware", "enabled"].forEach(
    (name) => {
      if (form.elements[name]) data[name] = form.elements[name].checked;
    }
  );
  ["port", "scan_interval", "slow_scan_interval"].forEach((name) => {
    if (data[name] === "" || data[name] === undefined) delete data[name];
    else data[name] = Number(data[name]);
  });
  // Blank secrets mean "unchanged", so drop them rather than clearing.
  if (!data.password) delete data.password;
  if (!data.token_secret) delete data.token_secret;
  if (!data.id) delete data.id;
  return data;
}

function editorMessage(text, kind) {
  const node = document.getElementById("editor-message");
  node.textContent = text;
  node.className = `editor-message ${kind}`;
  node.classList.remove("hidden");
}

document.getElementById("editor-test").addEventListener("click", async () => {
  editorMessage("Testing…", "");
  try {
    const result = await api("api/sources/test", {
      method: "POST",
      body: JSON.stringify(collect()),
    });
    editorMessage(`Connected to ${result.info.title}`, "good");
  } catch (err) {
    editorMessage(err.message, "bad");
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = collect();
  const id = form.elements.id.value;
  try {
    await api(id ? `api/sources/${id}` : "api/sources", {
      method: id ? "PUT" : "POST",
      body: JSON.stringify(data),
    });
    editor.classList.add("hidden");
    await load();
  } catch (err) {
    editorMessage(err.message, "bad");
  }
});

document.getElementById("editor-cancel").addEventListener("click", () => {
  editor.classList.add("hidden");
});
document.getElementById("field-type").addEventListener("change", () => {
  const type = form.elements.type.value;
  const defaults = DEFAULTS[type];
  form.elements.port.value = defaults.port;
  form.elements.scan_interval.value = defaults.scan_interval;
  applyTypeVisibility();
});
document.getElementById("field-auth").addEventListener("change", applyTypeVisibility);
document.getElementById("field-scope").addEventListener("change", applyTypeVisibility);
document.getElementById("add-source").addEventListener("click", () => openEditor(null));
document.getElementById("show-events").addEventListener("click", showEvents);

// -- monitor settings modal ---------------------------------------------

const monitorModal = document.getElementById("monitor");
const monitorForm = document.getElementById("monitor-form");
// Which line the modal is currently editing.
let editingMonitor = null;

const MONITOR_COLLECTION = {
  sensor: "sensors",
  binary_sensor: "binary_sensors",
  update: "updates",
};

function findMonitor(kind, key) {
  const source = state.detail;
  if (!source) return null;
  return (source[MONITOR_COLLECTION[kind]] || []).find((i) => i.key === key) || null;
}

function currentValueText(item, kind) {
  if (kind === "sensor") return formatValue(item);
  if (kind === "binary_sensor") return formatBinary(item).text;
  return item.installed_version || "—";
}

function openMonitor(kind, key) {
  const item = findMonitor(kind, key);
  if (!item) return;
  editingMonitor = { kind, key, sourceId: state.detail.id };
  const override = overrideFor(state.detail, key);

  document.getElementById("monitor-title").textContent = item.name;
  document.getElementById("monitor-key").textContent = key;
  document.getElementById("monitor-value").textContent = currentValueText(item, kind);

  const reason = document.getElementById("monitor-reason");
  reason.textContent = item.reason || "";
  reason.classList.toggle("hidden", !item.reason);

  monitorForm.elements.muted.checked = override.muted;
  ["warn_above", "warn_below", "critical_above", "critical_below"].forEach((name) => {
    const value = override.thresholds[name];
    monitorForm.elements[name].value = value === undefined ? "" : value;
  });

  // Thresholds are only meaningful for a number; a problem/OK line or a
  // version string has nothing to compare against.
  const numeric = kind === "sensor" && typeof item.value === "number";
  document.getElementById("monitor-thresholds").classList.toggle("hidden", !numeric);
  document.getElementById("monitor-no-thresholds").classList.toggle("hidden", numeric);

  monitorMessage("");
  monitorModal.classList.remove("hidden");
}

function closeMonitor() {
  editingMonitor = null;
  monitorModal.classList.add("hidden");
}

function monitorMessage(text, kind = "bad") {
  const node = document.getElementById("monitor-message");
  node.textContent = text || "";
  node.className = text ? `editor-message ${kind}` : "editor-message hidden";
}

monitorForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!editingMonitor) return;
  const { sourceId, key } = editingMonitor;

  const thresholds = {};
  ["warn_above", "warn_below", "critical_above", "critical_below"].forEach((name) => {
    const raw = monitorForm.elements[name].value.trim();
    if (raw !== "") thresholds[name] = Number(raw);
  });

  try {
    await api(`api/sources/${sourceId}/overrides/${encodeURIComponent(key)}`, {
      method: "PUT",
      body: JSON.stringify({
        muted: monitorForm.elements.muted.checked,
        thresholds,
      }),
    });
    closeMonitor();
    await loadDetail();
  } catch (err) {
    monitorMessage(err.message);
  }
});

document.getElementById("monitor-reset").addEventListener("click", async () => {
  if (!editingMonitor) return;
  const { sourceId, key } = editingMonitor;
  try {
    await api(`api/sources/${sourceId}/overrides/${encodeURIComponent(key)}`, {
      method: "DELETE",
    });
    closeMonitor();
    await loadDetail();
  } catch (err) {
    monitorMessage(err.message);
  }
});

document.getElementById("monitor-cancel").addEventListener("click", closeMonitor);

// Delegated from <main> so it covers both the list and the detail view.
document.querySelector("main").addEventListener("click", async (event) => {
  // Buttons are checked first: an Install button sits inside a monitor row,
  // and pressing it must not also open that row's settings.
  const button = event.target.closest("button[data-action]");
  if (!button) {
    const row = event.target.closest("tr[data-monitor]");
    if (row) openMonitor(row.dataset.kind, row.dataset.monitor);
    return;
  }

  const { action, id, key } = button.dataset;
  const source = state.sources.find((s) => s.id === id);

  try {
    if (action === "install" || action === "press") {
      const label = button.textContent;
      button.disabled = true;
      button.textContent = "Working…";
      try {
        await api("api/action", {
          method: "POST",
          body: JSON.stringify({
            source_id: id,
            kind: action === "install" ? "update" : "button",
            key,
          }),
        });
        banner(`${label} started`, "good");
        setTimeout(() => banner(""), 4000);
      } finally {
        // The next refresh replaces the row anyway; this only matters if the
        // action failed and the row is still the one the user clicked.
        button.disabled = false;
        button.textContent = label;
      }
      setTimeout(loadDetail, 2000);
    } else if (action === "view") {
      await showDetail(id);
    } else if (action === "edit") {
      openEditor(source);
    } else if (action === "refresh") {
      await api(`api/sources/${id}/refresh`, { method: "POST" });
      // Give the poll a moment to land before reading it back.
      setTimeout(load, 1500);
    } else if (action === "delete") {
      const label = source ? source.name : "this device";
      if (!confirm(`Remove ${label}? Its Home Assistant entities will disappear.`)) {
        return;
      }
      await api(`api/sources/${id}`, { method: "DELETE" });
      showList();
    }
  } catch (err) {
    banner(err.message);
  }
});

document.getElementById("copy-token").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(state.token);
    banner("API token copied to the clipboard", "good");
    setTimeout(() => banner(""), 3000);
  } catch {
    banner("Could not copy; select the token and copy it manually");
  }
});

// Monitor rows are focusable, so keep them operable without a mouse.
document.querySelector("main").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  const row = event.target.closest("tr[data-monitor]");
  if (!row) return;
  event.preventDefault();
  openMonitor(row.dataset.kind, row.dataset.monitor);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !monitorModal.classList.contains("hidden")) {
    closeMonitor();
  }
});

// Shown in the header so the running build is never in doubt: an add-on store
// whose repository URL has stopped resolving keeps serving the old one.
api("api/health")
  .then((health) => {
    if (health.version) {
      document.getElementById("version").textContent = `v${health.version}`;
    }
  })
  .catch(() => {});

load();
setInterval(load, REFRESH_MS);
