// radio-keyer web UI. Plain ES module, no build step.

/**
 * @typedef {{actions: string[], inherited: boolean}} PressView
 * @typedef {{position: number, short: PressView, long: PressView}} KeyView
 * @typedef {{name: string, label: string}} ProfileView
 * @typedef {{type: "state", callsign: string, rig: string, keypad_connected: boolean | null,
 *   profiles: ProfileView[], active_profile: string, keys: KeyView[], config_path: string,
 *   restart_required: string[]}} StateMsg
 * @typedef {{type: "status", ok: boolean, freq_hz?: number, mode?: string,
 *   passband_hz?: number, error?: string}} StatusMsg
 * @typedef {{type: "log", t: number, level: string, msg: string}} LogMsg
 * @typedef {{type: "log_history", entries: LogMsg[]}} LogHistoryMsg
 */

const LOG_MAX = 300;
const RECONNECT_MAX_MS = 5000;

/** @param {string} id @returns {HTMLElement} */
const $ = (id) => {
  const el = document.getElementById(id);
  if (el === null) throw new Error(`missing #${id}`);
  return el;
};

/**
 * @param {string} tag
 * @param {Record<string, string>} [attrs]
 * @param {(Node | string)[]} [children]
 */
const h = (tag, attrs = {}, children = []) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  el.append(...children);
  return el;
};

/** 14030000 → "14.030.000" */
const formatFreq = (hz) => {
  const khz = Math.floor(hz / 1000);
  const mhz = Math.floor(khz / 1000);
  const pad = (n) => String(n).padStart(3, "0");
  return `${mhz}.${pad(khz % 1000)}.${pad(hz % 1000)}`;
};

/**
 * JSON request; returns the parsed body or throws with the server's message.
 * @param {string} method @param {string} url @param {unknown} [body]
 */
const api = async (method, url, body) => {
  const res = await fetch(url, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try {
    data = await res.json();
  } catch {
    // non-JSON error body; fall back to the status text below
  }
  if (!res.ok && data.ok === undefined) {
    const detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? res.statusText);
    throw new Error(`${res.status}: ${detail}`);
  }
  return data;
};

// --- indicators -----------------------------------------------------------

/** @param {string} id @param {"on" | "off" | "err" | "na"} state @param {string} [title] */
const setPill = (id, state, title = "") => {
  const el = $(id);
  el.dataset.state = state;
  el.title = title;
};

// --- state / keys ---------------------------------------------------------

let activeProfile = "";

/** @param {StateMsg} msg */
const renderState = (msg) => {
  activeProfile = msg.active_profile;
  $("callsign").textContent = msg.callsign;
  $("rig-desc").textContent = msg.rig;
  $("config-path").textContent = msg.config_path;
  if (msg.keypad_connected === null) setPill("ind-keypad", "na");
  else setPill("ind-keypad", msg.keypad_connected ? "on" : "off", msg.keypad_connected ? "connected" : "not connected");

  const banner = $("restart-banner");
  banner.hidden = msg.restart_required.length === 0;
  banner.textContent = `Saved. Restart radio-keyer to apply: ${msg.restart_required.join(", ")} settings.`;

  $("profiles").replaceChildren(
    ...msg.profiles.map((p) => {
      const btn = h("button", { type: "button", "aria-pressed": String(p.name === msg.active_profile), title: p.name }, [p.label]);
      btn.addEventListener("click", () => selectProfile(p.name));
      return btn;
    }),
  );

  $("keys").replaceChildren(...msg.keys.map(renderKey));
};

/** @param {KeyView} key */
const renderKey = (key) => {
  const edit = h("button", { type: "button", class: "ghost icon", title: `Edit key ${key.position}`, "aria-label": `Edit key ${key.position}` }, ["✎"]);
  edit.addEventListener("click", () => openEditor(activeProfile, key.position));
  const card = h("div", { class: "key", id: `key-${key.position}` }, [
    h("div", { class: "key-head" }, [`Key ${key.position}`, edit]),
  ]);
  for (const kind of /** @type {const} */ (["short", "long"])) {
    const view = key[kind];
    const empty = view.actions.length === 0;
    const label = h("span", { class: "kind" }, [kind === "short" ? "short" : "⏵⏵ long"]);
    if (view.inherited) label.append(" ", h("span", { class: "tag" }, ["global"]));
    const btn = h("button", { type: "button", class: empty ? "press empty" : "press" }, [
      label,
      h("span", { class: "action" }, [empty ? "unassigned" : view.actions.join(" → ")]),
    ]);
    if (empty) btn.setAttribute("disabled", "");
    btn.addEventListener("click", () => pressKey(key.position, kind));
    card.append(btn);
  }
  return card;
};

/** @param {number} position @param {"short" | "long"} kind */
const pressKey = async (position, kind) => {
  const card = document.getElementById(`key-${position}`);
  card?.classList.remove("flash", "fail");
  try {
    const res = await api("POST", "/api/press", { position, kind });
    card?.classList.add(res.ok ? "flash" : "fail");
  } catch (err) {
    card?.classList.add("fail");
    addLog({ type: "log", t: Date.now() / 1000, level: "ERROR", msg: String(err) });
  }
  setTimeout(() => card?.classList.remove("flash", "fail"), 600);
};

/** @param {string} name */
const selectProfile = async (name) => {
  try {
    await api("POST", "/api/profile", { name });
  } catch (err) {
    addLog({ type: "log", t: Date.now() / 1000, level: "ERROR", msg: String(err) });
  }
};

// --- rig status -----------------------------------------------------------

/** @param {StatusMsg} msg */
const renderStatus = (msg) => {
  const freq = $("freq");
  const error = $("rig-error");
  if (msg.ok && msg.freq_hz !== undefined) {
    freq.textContent = formatFreq(msg.freq_hz);
    freq.classList.remove("stale");
    $("mode").textContent = msg.mode ?? "";
    $("passband").textContent = msg.passband_hz ? `${msg.passband_hz} Hz` : "";
    error.hidden = true;
    setPill("ind-rig", "on", "rig responding");
  } else {
    freq.classList.add("stale");
    error.hidden = false;
    error.textContent = msg.error ?? "rig not reachable";
    setPill("ind-rig", "err", msg.error ?? "");
  }
};

// --- log ------------------------------------------------------------------

/** @param {LogMsg} entry */
const addLog = (entry) => {
  const list = $("log");
  const time = new Date(entry.t * 1000).toLocaleTimeString([], { hour12: false });
  list.append(h("li", { class: entry.level }, [h("time", {}, [time]), entry.msg]));
  while (list.childElementCount > LOG_MAX) list.firstElementChild?.remove();
  list.scrollTop = list.scrollHeight;
};

$("log-clear").addEventListener("click", () => $("log").replaceChildren());

// --- websocket ------------------------------------------------------------

let reconnectMs = 500;

const connect = () => {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.addEventListener("open", () => {
    reconnectMs = 500;
    setPill("ind-ws", "on", "connected to radio-keyer");
  });
  ws.addEventListener("message", (ev) => {
    /** @type {StateMsg | StatusMsg | LogMsg | LogHistoryMsg} */
    const msg = JSON.parse(ev.data);
    switch (msg.type) {
      case "state": renderState(msg); break;
      case "status": renderStatus(msg); break;
      case "log": addLog(msg); break;
      case "log_history":
        $("log").replaceChildren();
        msg.entries.forEach(addLog);
        break;
      default: break;
    }
  });
  ws.addEventListener("close", () => {
    setPill("ind-ws", "err", "disconnected — retrying");
    setPill("ind-rig", "off");
    setTimeout(connect, reconnectMs);
    reconnectMs = Math.min(reconnectMs * 2, RECONNECT_MAX_MS);
  });
};

// --- config editor --------------------------------------------------------

const cfgText = /** @type {HTMLTextAreaElement} */ ($("cfg-text"));
const cfgResult = $("cfg-result");
let savedText = "";

/** @param {"ok" | "err" | null} kind @param {string} [text] */
const showCfgResult = (kind, text = "") => {
  cfgResult.hidden = kind === null;
  cfgResult.className = `cfg-result ${kind ?? ""}`;
  cfgResult.textContent = text;
};

const loadConfig = async () => {
  if (cfgText.value !== savedText && !confirm("Discard unsaved config changes?")) return;
  try {
    const data = await api("GET", "/api/config");
    savedText = data.text;
    cfgText.value = data.text;
    showCfgResult(null);
  } catch (err) {
    showCfgResult("err", String(err));
  }
};

const checkConfig = async () => {
  try {
    const res = await api("POST", "/api/config/check", { text: cfgText.value });
    showCfgResult(res.ok ? "ok" : "err", res.ok ? "Config OK" : res.error);
  } catch (err) {
    showCfgResult("err", String(err));
  }
};

const saveConfig = async () => {
  const text = cfgText.value;
  try {
    const res = await api("PUT", "/api/config", { text });
    if (!res.ok) {
      showCfgResult("err", res.error);
      return;
    }
    savedText = text;
    const restart = res.restart_required.length > 0 ? ` (restart needed for: ${res.restart_required.join(", ")})` : "";
    showCfgResult("ok", `Saved and applied${restart}. Previous version kept as .bak`);
  } catch (err) {
    showCfgResult("err", String(err));
  }
};

$("cfg-reload").addEventListener("click", loadConfig);
$("cfg-check").addEventListener("click", checkConfig);
$("cfg-save").addEventListener("click", saveConfig);

cfgText.addEventListener("keydown", (ev) => {
  if ((ev.ctrlKey || ev.metaKey) && ev.key === "s") {
    ev.preventDefault();
    saveConfig();
  } else if (ev.key === "Tab" && !ev.shiftKey) {
    ev.preventDefault();
    cfgText.setRangeText("  ", cfgText.selectionStart, cfgText.selectionEnd, "end");
  }
});

window.addEventListener("beforeunload", (ev) => {
  if (cfgText.value !== savedText) ev.preventDefault();
});

// --- per-key editor -------------------------------------------------------

/**
 * @typedef {{profiles: string[], profile_targets: string[], presets: string[], bands: string[],
 *   modes: string[], cw_via: string[], cw_fields: string[], num_keys: number}} Schema
 * @typedef {Record<string, string | number | boolean | null>} ActionDict
 * @typedef {{profile: string, position: number, short: ActionDict[] | null,
 *   long: ActionDict[] | null}} KeyData
 * @typedef {"text" | "int" | "select"} FieldType
 * @typedef {{name: string, type: FieldType, label: string, optional?: boolean,
 *   options?: (s: Schema) => string[], min?: number, max?: number, placeholder?: string}} FieldSpec
 */

/** Form fields per action type. The first field holds the action's own value. */
/** @type {Record<string, FieldSpec[]>} */
const ACTION_FIELDS = {
  cw: [
    { name: "cw", type: "text", label: "text", placeholder: "CQ {call} {call} K" },
    { name: "via", type: "select", label: "via", optional: true, options: (s) => s.cw_via },
  ],
  cw_memory: [{ name: "cw_memory", type: "int", label: "memory", min: 1, max: 10 }],
  cw_stop: [],
  band: [{ name: "band", type: "select", label: "band", options: (s) => s.bands }],
  freq: [
    { name: "freq", type: "int", label: "Hz", min: 1, placeholder: "14074000" },
    { name: "mode", type: "select", label: "mode", optional: true, options: (s) => s.modes },
  ],
  mode: [{ name: "mode", type: "select", label: "mode", options: (s) => s.modes }],
  cat: [{ name: "cat", type: "text", label: "CAT", placeholder: "FA;" }],
  cat_hex: [{ name: "cat_hex", type: "text", label: "hex", placeholder: "FE FE 94 E0 03 FD" }],
  profile: [{ name: "profile", type: "select", label: "profile", options: (s) => s.profile_targets }],
  preset: [{ name: "preset", type: "select", label: "preset", options: (s) => s.presets }],
  delay_ms: [{ name: "delay_ms", type: "int", label: "ms", min: 0, max: 60000 }],
};
const ACTION_TYPES = Object.keys(ACTION_FIELDS);

const ke = /** @type {HTMLDialogElement} */ ($("ke"));
const keProfile = /** @type {HTMLSelectElement} */ ($("ke-profile"));
const keResult = $("ke-result");
/** @type {Schema | null} */
let schema = null;
let kePosition = 0;

/** @param {"ok" | "err" | null} kind @param {string} [text] */
const showKeResult = (kind, text = "") => {
  keResult.hidden = kind === null;
  keResult.className = `cfg-result ${kind ?? ""}`;
  keResult.textContent = text;
};

/**
 * Which action type a stored action is (freq/cw may carry an extra key).
 * @param {ActionDict} action
 */
const actionType = (action) => ACTION_TYPES.find((t) => t in action) ?? Object.keys(action)[0] ?? "cw";

/**
 * @param {string[]} options @param {string} value @param {boolean} optional
 * @returns {HTMLSelectElement}
 */
const makeSelect = (options, value, optional) => {
  const sel = /** @type {HTMLSelectElement} */ (h("select"));
  if (optional) sel.append(h("option", { value: "" }, ["—"]));
  const all = value !== "" && !options.includes(value) ? [...options, value] : options;
  sel.append(...all.map((o) => h("option", { value: o }, [o])));
  sel.value = value;
  return sel;
};

/** @param {FieldSpec} spec @param {ActionDict} action */
const makeField = (spec, action) => {
  const raw = action[spec.name];
  let value = raw === null || raw === undefined ? "" : String(raw);
  if (spec.name === "mode") value = value.toUpperCase();
  /** @type {HTMLInputElement | HTMLSelectElement} */
  let input;
  if (spec.type === "select" && schema !== null && spec.options !== undefined) {
    input = makeSelect(spec.options(schema), value, spec.optional ?? false);
  } else {
    input = /** @type {HTMLInputElement} */ (h("input", { type: spec.type === "int" ? "number" : "text" }));
    if (spec.min !== undefined) input.min = String(spec.min);
    if (spec.max !== undefined) input.max = String(spec.max);
    if (spec.placeholder !== undefined) input.placeholder = spec.placeholder;
    input.value = value;
  }
  input.dataset.field = spec.name;
  input.dataset.type = spec.type;
  input.dataset.optional = String(spec.optional ?? false);
  input.setAttribute("aria-label", spec.label);
  return h("label", { class: `field field-${spec.type}` }, [h("span", {}, [spec.label]), input]);
};

/** @param {HTMLElement} row @param {string} type @param {ActionDict} action */
const fillRow = (row, type, action) => {
  const fields = row.querySelector(".fields");
  fields?.replaceChildren(...(ACTION_FIELDS[type] ?? []).map((spec) => makeField(spec, action)));
};

/** @param {ActionDict} action */
const makeRow = (action) => {
  const type = actionType(action);
  const typeSel = makeSelect(ACTION_TYPES, type, false);
  typeSel.classList.add("action-type");
  typeSel.setAttribute("aria-label", "action");
  const row = h("li", { class: "action-row" }, [typeSel, h("div", { class: "fields" })]);
  /** @param {string} label @param {string} title @param {() => void} fn */
  const tool = (label, title, fn) => {
    const b = h("button", { type: "button", class: "ghost icon", title, "aria-label": title }, [label]);
    b.addEventListener("click", fn);
    return b;
  };
  row.append(
    h("div", { class: "row-tools" }, [
      tool("↑", "Move up", () => row.previousElementSibling?.before(row)),
      tool("↓", "Move down", () => row.nextElementSibling?.after(row)),
      tool("✕", "Remove", () => row.remove()),
    ]),
  );
  typeSel.addEventListener("change", () => fillRow(row, typeSel.value, {}));
  fillRow(row, type, action);
  return row;
};

/** @param {"short" | "long"} kind @param {ActionDict[] | null} actions @param {string} profile */
const makePress = (kind, actions, profile) => {
  const list = h("ol", { class: "action-list", "data-kind": kind }, (actions ?? []).map(makeRow));
  const add = h("button", { type: "button", class: "ghost" }, ["+ Add action"]);
  add.addEventListener("click", () => list.append(makeRow({ cw: "" })));
  const hint = profile === "global" ? "Empty = unassigned." : "Empty = use the global profile.";
  return h("fieldset", { class: "press-edit" }, [
    h("legend", {}, [kind === "short" ? "Short press" : "⏵⏵ Long press"]),
    list,
    h("div", { class: "press-foot" }, [add, h("span", { class: "muted small" }, [`${hint} Actions run top to bottom.`])]),
  ]);
};

/** @param {HTMLElement} row @returns {ActionDict} */
const collectRow = (row) => {
  const type = /** @type {HTMLSelectElement} */ (row.querySelector(".action-type")).value;
  /** @type {ActionDict} */
  const action = type === "cw_stop" ? { cw_stop: true } : {};
  for (const el of row.querySelectorAll("[data-field]")) {
    const input = /** @type {HTMLInputElement | HTMLSelectElement} */ (el);
    const name = input.dataset.field ?? "";
    const value = input.value.trim();
    if (value === "") {
      if (input.dataset.optional !== "true") action[name] = null; // server reports it as missing
      continue;
    }
    action[name] = input.dataset.type === "int" ? Number(value) : value;
  }
  return action;
};

/** @param {"short" | "long"} kind @returns {ActionDict[] | null} */
const collectPress = (kind) => {
  const rows = [...document.querySelectorAll(`#ke-presses .action-list[data-kind="${kind}"] > li`)];
  return rows.length === 0 ? null : rows.map((r) => collectRow(/** @type {HTMLElement} */ (r)));
};

/** @param {string} profile */
const loadKey = async (profile) => {
  showKeResult(null);
  $("ke-title").textContent = `Edit key ${kePosition}`;
  try {
    /** @type {KeyData} */
    const data = await api("GET", `/api/keys/${encodeURIComponent(profile)}/${kePosition}`);
    $("ke-presses").replaceChildren(makePress("short", data.short, profile), makePress("long", data.long, profile));
  } catch (err) {
    $("ke-presses").replaceChildren();
    showKeResult("err", String(err));
  }
};

/** @param {string} profile @param {number} position */
const openEditor = async (profile, position) => {
  try {
    schema = await api("GET", "/api/schema");
  } catch (err) {
    addLog({ type: "log", t: Date.now() / 1000, level: "ERROR", msg: String(err) });
    return;
  }
  kePosition = position;
  keProfile.replaceChildren(...schema.profiles.map((p) => h("option", { value: p }, [p])));
  keProfile.value = profile;
  await loadKey(profile);
  if (!ke.open) ke.showModal();
};

const saveKey = async () => {
  const body = { short: collectPress("short"), long: collectPress("long") };
  try {
    const res = await api("PUT", `/api/keys/${encodeURIComponent(keProfile.value)}/${kePosition}`, body);
    if (!res.ok) {
      showKeResult("err", res.error);
      return;
    }
    ke.close();
    if (cfgText.value === savedText) {
      await loadConfig();
    } else {
      showCfgResult("err", "config.yaml was changed by the key editor. Your unsaved text edits would overwrite it: Revert to load the new file.");
    }
  } catch (err) {
    showKeResult("err", String(err));
  }
};

keProfile.addEventListener("change", () => loadKey(keProfile.value));
$("ke-save").addEventListener("click", saveKey);
$("ke-cancel").addEventListener("click", () => ke.close());

connect();
loadConfig();
