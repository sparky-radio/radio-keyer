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

/** @param {StateMsg} msg */
const renderState = (msg) => {
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
  const card = h("div", { class: "key", id: `key-${key.position}` }, [
    h("div", { class: "key-head" }, [`Key ${key.position}`]),
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

connect();
loadConfig();
