"use strict";

// Knobs shown in the hand-built basic block; everything else is generated from
// the parser schema so the form never restates a default the CLI already owns.
const BASIC_DESTS = new Set([
  "speed",
  "target_lufs",
  "denoise",
  "silence_threshold",
  "silence_bias",
  "arnndn_model",
  "arnndn_mix",
  "cq",
  "crf",
  "encoder",
  "if_exists",
  "start",
  "limit",
  "mono",
  "output",
]);
// The preview sweep is a separate flow; live preview is wired by the server.
const SKIPPED_GROUPS = new Set(["preview sweep", "live preview"]);

// Measured on a whiteboard lecture, 1080p30, through the pipeline: at cq 23 it
// wrote 2.8 GB/h and at cq 28 1.4 GB/h, and the handwriting on the board is
// indistinguishable between them. Sizes are for the GPU encoder; libx264 writes
// roughly half as much for the same look.
const QUALITY_LEVELS = [
  { key: "q.max", cq: 23, crf: 20, gpu: 2.8, cpu: 1.3, hevc: 1.4 },
  { key: "q.high", cq: 26, crf: 22, gpu: 1.9, cpu: 0.95, hevc: 1.0 },
  { key: "q.normal", cq: 28, crf: 23, gpu: 1.4, cpu: 0.8, hevc: 0.8 },
  { key: "q.compact", cq: 32, crf: 26, gpu: 0.77, cpu: 0.72, hevc: 0.5 },
  { key: "q.min", cq: 36, crf: 29, gpu: 0.45, cpu: 0.5, hevc: 0.35 },
];

const ACTIVE_STATUSES = new Set(["queued", "running"]);
const SETTINGS_KEY = "lecturecut.settings.v1";
// Deliberately not remembered: they belong to one particular file, and silently
// reusing them would quietly process 60 seconds of the next lecture.
const NEVER_REMEMBERED = new Set(["set-output-name", "set-start", "set-limit"]);
const JOB_POLL_MS = 1500;

const state = {
  schema: null,
  defaults: {},
  files: [],
  dirs: [],
  selected: null,
  primary: null,
  browsing: null,
  checked: new Set(),
  jobs: [],
  watching: null,
  player: { jobId: null, mode: null, hls: null },
  stream: null,
  poller: null,
};

const el = (id) => document.getElementById(id);

// Sent on every request. The server refuses changes without it, and a page on
// another site cannot add a custom header without a CORS approval it never gets.
const GUARD_HEADER = "X-LectureCut";

async function api(path, options) {
  const request = { ...(options || {}) };
  request.headers = { ...(request.headers || {}), [GUARD_HEADER]: "1" };
  const response = await fetch(path, request);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body.detail) detail = body.detail;
    } catch (error) {
      /* keep the status line */
    }
    throw new Error(detail);
  }
  return response.json();
}

function formatSize(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 && unit > 0 ? 1 : 0)} ${units[unit]}`;
}

function formatDuration(seconds) {
  if (!seconds && seconds !== 0) return "";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

function formatEta(seconds) {
  if (seconds === null || seconds === undefined) return "";
  if (seconds < 60) return t("eta.lessMinute");
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return t("eta.minutes", { n: minutes });
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? t("eta.hoursMinutes", { h: hours, m: rest }) : t("eta.hours", { h: hours });
}

function clockIn(seconds) {
  const at = new Date(Date.now() + seconds * 1000);
  const locale = currentLang() === "ru" ? "ru-RU" : "en-GB";
  const time = at.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });
  const sameDay = at.toDateString() === new Date().toDateString();
  return sameDay
    ? time
    : `${at.toLocaleDateString(locale, { day: "numeric", month: "short" })} ${time}`;
}

function renderQueueSummary(queue) {
  const box = el("queue-summary");
  if (!queue || !queue.pending) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  const known = queue.pending - queue.unknown;
  const parts = [t("queue.pending", { n: queue.pending })];
  if (known > 0) {
    // With unknown lengths among them, the sum is only a floor.
    const approx = queue.unknown ? t("queue.atLeast") : "≈ ";
    parts.push(t("queue.remaining", { approx, eta: formatEta(queue.remaining_seconds) }));
    if (!queue.unknown) {
      parts.push(t("queue.finishBy", { time: clockIn(queue.remaining_seconds) }));
    }
  }
  if (queue.unknown) parts.push(t("queue.unknown", { n: queue.unknown }));
  let text = parts.join(" · ");
  if (!queue.learned_from) {
    text += t("queue.learning");
  }
  box.textContent = text;
}

function baseName(path) {
  return String(path).split("/").pop();
}

function dirName(path) {
  const parts = String(path).split("/");
  parts.pop();
  return parts.join("/") || "/";
}

// ---------------------------------------------------------------- settings form

function fieldId(dest) {
  return `adv-${dest}`;
}

function buildAdvanced() {
  const host = el("advanced-settings");
  host.textContent = "";
  for (const group of state.schema.groups) {
    if (SKIPPED_GROUPS.has(group.title)) continue;
    const fields = group.fields.filter((field) => !BASIC_DESTS.has(field.dest));
    if (!fields.length) continue;

    const title = document.createElement("div");
    title.className = "group-title";
    title.textContent = group.title;
    host.appendChild(title);

    for (const field of fields) {
      const label = document.createElement("label");
      label.title = field.help;
      if (field.kind === "flag") {
        label.className = "inline-label";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.id = fieldId(field.dest);
        input.checked = Boolean(field.default);
        const text = document.createElement("span");
        text.textContent = field.option;
        label.append(input, text);
      } else {
        label.textContent = field.option;
        let input;
        if (field.kind === "choice") {
          input = document.createElement("select");
          for (const choice of field.choices || []) {
            const option = document.createElement("option");
            option.value = choice;
            option.textContent = choice;
            input.appendChild(option);
          }
          input.value = field.default ?? "";
        } else {
          input = document.createElement("input");
          input.type = field.kind === "number" || field.kind === "integer" ? "number" : "text";
          if (field.kind === "number") input.step = "any";
          input.value = field.default === null || field.default === undefined ? "" : field.default;
          input.placeholder = String(field.default ?? "");
        }
        input.id = fieldId(field.dest);
        label.appendChild(input);
      }
      host.appendChild(label);
    }
  }
}

function applyDefaults() {
  const d = state.defaults;
  el("set-speed").value = d.speed;
  el("set-target-lufs").value = d.target_lufs;
  el("set-mono").checked = Boolean(d.mono);
  const denoise = el("set-denoise");
  denoise.textContent = "";
  for (const choice of d.__denoise_choices || []) {
    const option = document.createElement("option");
    option.value = choice;
    option.textContent = choice;
    denoise.appendChild(option);
  }
  denoise.value = d.denoise;
  el("set-arnndn-mix").value = d.arnndn_mix ?? 1;
  renderQualityLevels();
  el("set-encoder").value = ["libx264", "hevc_nvenc"].includes(d.encoder) ? d.encoder : "auto";
  syncQualityHint();
  syncArnndnMix();
  renderModels();
  syncDenoiseHelp();
  const auto = String(d.silence_threshold).toLowerCase() === "auto";
  el("set-silence-auto").checked = auto;
  el("set-silence-threshold").disabled = auto;
  el("set-silence-threshold").value = auto ? "" : d.silence_threshold;
  el("set-silence-bias").value = d.silence_bias ?? 0;
  syncSilenceControls();
}

function modelLabel(model) {
  // The upstream table maps what a model expects to hear against what it expects
  // to filter out; the core sends those two keys and the wording lives here.
  const parts = [model.name || model.file];
  if (model.signal && model.noise) {
    parts.push(`${t(`signal.${model.signal}`)} + ${t(`noise.${model.noise}`)}`);
  }
  if (model.recommended) parts.push(t("den.recommended"));
  return parts.join(" — ");
}

function currentQuality() {
  const index = Number(el("set-quality").value);
  return QUALITY_LEVELS[index] || QUALITY_LEVELS[2];
}

function renderQualityLevels(rebuild) {
  const select = el("set-quality");
  if (select.options.length && !rebuild) return;
  const chosen = select.value;
  select.textContent = "";
  QUALITY_LEVELS.forEach((level, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = t(level.key);
    select.appendChild(option);
  });
  if (chosen) {
    select.value = chosen;
    return;
  }
  // Whatever the defaults say, so the page starts where the CLI would.
  const fromDefaults = QUALITY_LEVELS.findIndex((level) => level.cq === state.defaults.cq);
  select.value = String(fromDefaults >= 0 ? fromDefaults : 2);
}

function syncQualityHint() {
  const level = currentQuality();
  const encoder = el("set-encoder").value;
  const perHour =
    encoder === "libx264" ? level.cpu : encoder === "hevc_nvenc" ? level.hevc : level.gpu;
  const speed = Number(el("set-speed").value) || 1;
  const duration = state.selected && state.selected.duration;
  // The output is shorter than the input by the speed-up, and shorter again by
  // whatever silence is cut - so this is an upper bound.
  const bytes = duration ? (perHour * 1073741824 * duration) / speed / 3600 : 0;
  const forThisFile = bytes ? t("q.hintFile", { size: formatSize(bytes) }) : "";
  el("quality-hint").textContent = t("q.hint", {
    perHour: perHour.toFixed(1),
    file: forThisFile,
  });
}

function syncArnndnMix() {
  const share = Math.round(Number(el("set-arnndn-mix").value) * 100);
  el("arnndn-mix-value").textContent = `${share}%`;
}

function syncDenoiseHelp() {
  const mode = el("set-denoise").value;
  const help = t(`den.${mode}`);
  const models = state.schema.models || [];
  // arnndn is the best of the three and the only one that needs a file, so the
  // model picker appears exactly when it is about to be used.
  const needsModel = mode === "arnndn" || (mode === "auto" && models.length > 0);
  el("model-label").classList.toggle("hidden", !needsModel);
  el("arnndn-mix-label").classList.toggle("hidden", !needsModel);
  const missing = mode === "arnndn" && !models.length;
  el("denoise-help").textContent = missing ? t("den.noModel", { help }) : help;
  el("denoise-help").classList.toggle("warn", missing);
}

function renderModels() {
  const select = el("set-arnndn-model");
  const previous = select.value;
  select.textContent = "";
  const models = state.schema.models || [];
  if (!models.length) {
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = t("den.modelsNone");
    select.appendChild(empty);
    select.disabled = true;
  } else {
    select.disabled = false;
    for (const model of models) {
      const option = document.createElement("option");
      option.value = model.path;
      option.textContent = modelLabel(model);
      option.title = model.path;
      select.appendChild(option);
    }
    select.value = previous || models[0].path;
  }
  const pending = (state.schema.catalogue || []).filter((m) => !m.installed);
  const total = pending.reduce((sum, m) => sum + m.size, 0);
  el("fetch-model").classList.toggle("hidden", models.length > 0);
  el("fetch-all-models").classList.toggle("hidden", pending.length === 0);
  el("fetch-all-models").textContent = pending.length
    ? t("den.fetchMore", { n: pending.length, kb: Math.round(total / 1024) })
    : t("set.all");
  el("model-hint").textContent = t("den.modelHint", { dir: state.schema.model_dir });
}

async function fetchModels(keys) {
  const buttons = [el("fetch-model"), el("fetch-all-models")];
  for (const button of buttons) button.disabled = true;
  el("model-hint").textContent = t("den.downloading");
  try {
    const data = await api("/api/models", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keys }),
    });
    state.schema.models = data.models;
    state.schema.catalogue = data.catalogue;
    renderModels();
    syncDenoiseHelp();
  } catch (error) {
    el("model-hint").textContent = t("den.downloadFailed", { error: error.message });
    el("model-hint").classList.add("warn");
  } finally {
    for (const button of buttons) button.disabled = false;
  }
}

function syncSilenceControls() {
  const auto = el("set-silence-auto").checked;
  el("set-silence-threshold").disabled = auto;
  // The bias shifts the calibrated value, so it means nothing for a fixed one.
  el("silence-bias-label").classList.toggle("disabled", !auto);
  el("silence-bias-value").textContent = Number(el("set-silence-bias").value).toFixed(1);
}

function renderDirs() {
  const select = el("set-output-dir");
  const previous = select.value;
  select.textContent = "";
  const auto = document.createElement("option");
  auto.value = "";
  auto.textContent = t("dir.nextToSource");
  select.appendChild(auto);
  for (const dir of state.dirs) {
    const option = document.createElement("option");
    option.value = dir.path;
    option.textContent = dir.label;
    select.appendChild(option);
  }
  if (previous) select.value = previous;
}

function outputPath() {
  const dir = el("set-output-dir").value;
  const name = el("set-output-name").value.trim();
  if (!dir && !name) return null;
  const base =
    name ||
    (state.selected ? baseName(state.selected.defaultOutput || state.selected.name) : "");
  if (!base) return null;
  const folder = dir || (state.selected ? dirName(state.selected.path) : "");
  return `${folder}/${base}`;
}

function collectSettings() {
  const settings = {};
  const put = (dest, value) => {
    if (value === "" || value === null || value === undefined) return;
    if (state.defaults[dest] === value) return;
    settings[dest] = value;
  };

  put("speed", Number(el("set-speed").value));
  put("target_lufs", Number(el("set-target-lufs").value));
  put("denoise", el("set-denoise").value);
  const quality = currentQuality();
  put("cq", quality.cq);
  put("crf", quality.crf);
  put("encoder", el("set-encoder").value);
  const model = el("set-arnndn-model").value;
  if (model && !el("model-label").classList.contains("hidden")) {
    settings.arnndn_model = model;
    put("arnndn_mix", Number(el("set-arnndn-mix").value));
  }
  if (el("set-mono").checked) settings.mono = true;

  const auto = el("set-silence-auto").checked;
  put("silence_threshold", auto ? "auto" : el("set-silence-threshold").value.trim());
  if (auto) put("silence_bias", Number(el("set-silence-bias").value));

  const start = el("set-start").value;
  if (start !== "") put("start", Number(start));
  const limit = el("set-limit").value;
  if (limit !== "") settings.limit = Number(limit);
  const output = outputPath();
  if (output) settings.output = output;

  for (const group of state.schema.groups) {
    if (SKIPPED_GROUPS.has(group.title)) continue;
    for (const field of group.fields) {
      if (BASIC_DESTS.has(field.dest)) continue;
      const input = el(fieldId(field.dest));
      if (!input) continue;
      if (field.kind === "flag") {
        if (input.checked !== Boolean(field.default)) settings[field.dest] = input.checked;
        continue;
      }
      const raw = input.value.trim();
      if (raw === "") continue;
      const value =
        field.kind === "number" || field.kind === "integer" ? Number(raw) : raw;
      if (value !== field.default) settings[field.dest] = value;
    }
  }
  // Repeating a run should not destroy the previous result; the core picks the
  // next free name instead. Advanced exposes --if-exists to change that.
  return settings;
}

// ------------------------------------------------------------ saved settings

function settingsControls() {
  return [
    ...document.querySelectorAll(
      "#basic-settings input, #basic-settings select, " +
        "#advanced-settings input, #advanced-settings select, #hide-processed"
    ),
  ].filter((node) => node.id && !NEVER_REMEMBERED.has(node.id));
}

function saveFormState() {
  const data = {};
  for (const node of settingsControls()) {
    data[node.id] = node.type === "checkbox" ? node.checked : node.value;
  }
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(data));
  } catch (error) {
    /* a full or disabled store is not worth interrupting the run for */
  }
}

function restoreFormState() {
  let data;
  try {
    data = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "null");
  } catch (error) {
    data = null;
  }
  if (!data) return;
  for (const node of settingsControls()) {
    if (!(node.id in data)) continue;
    if (node.type === "checkbox") node.checked = Boolean(data[node.id]);
    else node.value = data[node.id];
  }
  // A folder that has since disappeared leaves the select empty, which is the
  // "next to the source" entry - the same thing a fresh install would show.
  syncSilenceControls();
  syncArnndnMix();
  syncQualityHint();
}

function resetFormState() {
  try {
    localStorage.removeItem(SETTINGS_KEY);
  } catch (error) {
    /* nothing to clear */
  }
  applyDefaults();
  buildAdvanced();
  el("set-output-dir").value = "";
  el("set-output-name").value = "";
  el("hide-processed").checked = true;
  renderFiles();
  refreshPrimary();
}

// ------------------------------------------------------------------ file picker

function renderFiles() {
  const list = el("file-list");
  list.textContent = "";
  const matches = visibleFiles();
  if (!matches.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    const hidden = state.files.length - visibleFiles({ ignoreProcessed: true }).length;
    empty.textContent = state.files.length ? t("files.noMatch") : t("files.none");
    if (state.files.length && hidden > 0) {
      empty.textContent = t("files.allProcessed");
    }
    list.appendChild(empty);
    return;
  }
  for (const file of matches) {
    const item = document.createElement("li");
    if (state.checked.has(file.path)) item.classList.add("checked");
    if (state.primary === file.path) item.classList.add("active");

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = file.relative || file.name;
    name.title = file.path;
    item.appendChild(name);
    if (file.processed) {
      const done = document.createElement("span");
      done.className = "badge done";
      done.textContent = t("files.processed");
      done.title = t("files.processedTitle");
      item.appendChild(done);
    }
    const size = document.createElement("span");
    size.className = "muted small";
    size.textContent = formatSize(file.size);
    item.appendChild(size);

    // One way to choose things: the whole row toggles. A second mechanism next
    // to it only raises the question of which of the two actually counts.
    item.addEventListener("click", () => toggleFile(file));
    list.appendChild(item);
  }
}

function toggleFile(file) {
  if (state.checked.has(file.path)) {
    state.checked.delete(file.path);
    if (state.primary === file.path) state.primary = null;
  } else {
    state.checked.add(file.path);
    state.primary = file.path;
  }
  renderFiles();
  refreshPrimary();
}

function primaryFile() {
  const chosen = batchTargets();
  if (!chosen.length) return null;
  const byPath = chosen.find((file) => file.path === state.primary);
  return byPath || chosen[chosen.length - 1];
}

async function refreshPrimary() {
  const file = primaryFile();
  state.selected = file;
  updateConvertButton();
  const box = el("selected");
  if (!file) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  const chosen = batchTargets();
  el("selected-name").textContent =
    chosen.length > 1 ? t("files.selectedN", { n: chosen.length }) : file.name;
  el("selected-meta").textContent = `${formatSize(file.size)} · ${file.path}`;
  await describeFile(file);
}

function visibleFiles(options) {
  const filter = el("file-filter").value.trim().toLowerCase();
  const hideProcessed =
    !(options && options.ignoreProcessed) && el("hide-processed").checked;
  return state.files.filter((file) => {
    if (filter && !file.name.toLowerCase().includes(filter)) return false;
    // A file that already carries the tag is a result, not a source.
    if (hideProcessed && file.processed) return false;
    return true;
  });
}

function batchTargets() {
  return state.files.filter((file) => state.checked.has(file.path));
}

function updateConvertButton() {
  const targets = batchTargets();
  const button = el("convert");
  button.disabled = targets.length === 0;
  button.textContent =
    targets.length > 1 ? t("set.convertN", { n: targets.length }) : t("set.convert");
  el("checked-count").textContent = state.checked.size
    ? t("files.checked", { n: state.checked.size })
    : "";
  // One explicit name cannot serve a batch; the core names each output instead.
  const name = el("set-output-name");
  name.disabled = targets.length > 1;
  name.placeholder =
    targets.length > 1
      ? t("files.batchNames")
      : state.selected && state.selected.defaultOutput
        ? baseName(state.selected.defaultOutput)
        : t("set.outputNamePlaceholder");
}

async function describeFile(file) {
  try {
    const info = await api(`/api/probe?path=${encodeURIComponent(file.path)}`);
    file.defaultOutput = info.default_output;
    if (state.selected !== file) return;
    const parts = [formatSize(file.size)];
    file.duration = info.duration;
    if (info.duration) parts.push(formatDuration(info.duration));
    syncQualityHint();
    if (!info.has_audio) parts.push(t("files.noAudio"));
    if (!info.has_video) parts.push(t("files.noVideo"));
    el("selected-meta").textContent = `${parts.join(" · ")} · ${file.path}`;
    updateConvertButton();
  } catch (error) {
    if (state.selected === file) {
      el("selected-meta").textContent = `${formatSize(file.size)} · ${error.message}`;
    }
  }
}

async function loadFiles() {
  const data = await api("/api/files");
  state.files = data.files;
  state.schema.source_dir = data.source_dir;
  renderSourceDir();
  renderFiles();
}

// ------------------------------------------------------------ input folder

function renderSourceDir() {
  const path = state.schema.source_dir || "";
  const label = el("source-dir-path");
  label.textContent = path;
  label.title = path;

  const recent = (state.schema.recent_source_dirs || []).filter((dir) => dir !== path);
  const select = el("recent-source-dirs");
  select.textContent = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = recent.length ? t("folder.recent") : t("folder.recentNone");
  select.appendChild(placeholder);
  for (const dir of recent) {
    const option = document.createElement("option");
    option.value = dir;
    option.textContent = dir;
    select.appendChild(option);
  }
  select.disabled = recent.length === 0;
  el("source-dir-tools").classList.toggle("hidden", !state.schema.allow_browse);
}

async function navigateFolder(path) {
  const note = el("folder-note");
  try {
    const query = path ? `?path=${encodeURIComponent(path)}` : "";
    const data = await api(`/api/browse${query}`);
    state.browsing = data;
    el("folder-path").value = data.path;
    el("folder-up").disabled = !data.parent;
    note.classList.remove("warn");
    note.textContent = data.media_here
      ? t("folder.mediaHere", { n: data.media_here })
      : t("folder.mediaNone");

    const list = el("folder-list");
    list.textContent = "";
    if (!data.dirs.length) {
      const empty = document.createElement("li");
      empty.className = "muted";
      empty.textContent = t("folder.noSub");
      list.appendChild(empty);
    }
    for (const dir of data.dirs) {
      const item = document.createElement("li");
      item.textContent = dir.name;
      item.title = dir.path;
      item.addEventListener("click", () => navigateFolder(dir.path));
      list.appendChild(item);
    }
  } catch (error) {
    note.classList.add("warn");
    note.textContent = error.message;
  }
}

function openFolderBrowser() {
  el("folder-dialog").showModal();
  navigateFolder(state.schema.source_dir);
}

async function chooseFolder(path) {
  if (!path) return;
  try {
    const data = await api("/api/source-dir", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    state.schema.source_dir = data.source_dir;
    state.schema.recent_source_dirs = data.recent_source_dirs;
    state.files = data.files;
    // Selections belong to the folder they were made in.
    state.checked.clear();
    state.primary = null;
    renderSourceDir();
    renderFiles();
    refreshPrimary();
    // The new folder is now a place outputs may go, too.
    await loadDirs();
    if (el("folder-dialog").open) el("folder-dialog").close();
  } catch (error) {
    const message = t("folder.failed", { error: error.message });
    if (el("folder-dialog").open) {
      el("folder-note").classList.add("warn");
      el("folder-note").textContent = message;
    } else {
      el("watch").classList.remove("hidden");
      appendLog(message, true);
    }
  }
}

async function loadDirs() {
  const data = await api("/api/dirs");
  state.dirs = data.dirs;
  renderDirs();
}

function dropNote(text, isError) {
  el("dropzone").classList.toggle("warn", Boolean(isError));
  el("drop-note").textContent = text;
}

async function selectByPath(path) {
  const match = state.files.find((file) => file.path === path);
  if (match && !state.checked.has(match.path)) toggleFile(match);
  return Boolean(match);
}

async function handleDrop(fileHandle) {
  const here = state.files.find(
    (file) => file.name === fileHandle.name && file.size === fileHandle.size
  );
  if (here) {
    if (!state.checked.has(here.path)) toggleFile(here);
    dropNote(t("drop.selected", { name: fileHandle.name }));
    return;
  }

  // The browser withheld the path, but the file is on this machine: look for it
  // where the server can already read, rather than copying it over localhost.
  dropNote(t("drop.searching", { name: fileHandle.name }));
  const found = await api(
    `/api/locate?name=${encodeURIComponent(fileHandle.name)}&size=${fileHandle.size}`
  );
  if (found.found) {
    await chooseFolder(found.dir);
    await selectByPath(found.path);
    dropNote(t("drop.selectedFrom", { name: fileHandle.name, dir: found.dir }));
    return;
  }

  if (!state.schema.allow_upload) {
    dropNote(t("drop.uploadDisabled", { name: fileHandle.name }), true);
    return;
  }
  const size = formatSize(fileHandle.size);
  const proceed = window.confirm(t("drop.confirm", { name: fileHandle.name, size }));
  if (!proceed) {
    dropNote(t("drop.pickFolder"), true);
    return;
  }

  dropNote(t("drop.uploading", { name: fileHandle.name, size }));
  const uploaded = await api(`/api/upload?name=${encodeURIComponent(fileHandle.name)}`, {
    method: "POST",
    body: fileHandle,
  });
  // The upload lands in its own folder, so switch to it or the file stays unseen.
  await chooseFolder(uploaded.dir);
  await selectByPath(uploaded.path);
  dropNote(t("drop.uploaded", { name: fileHandle.name }));
}

// ----------------------------------------------------------------- job queue

function statusLabel(status) {
  return t(`status.${status}`);
}

function renderJobs() {
  const list = el("job-list");
  list.textContent = "";
  if (!state.jobs.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = t("queue.empty");
    list.appendChild(empty);
    return;
  }
  for (const job of state.jobs) {
    const item = document.createElement("li");
    item.className = `job ${job.status}`;
    if (job.id === state.watching) item.classList.add("active");

    const head = document.createElement("div");
    head.className = "job-head";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = baseName(job.source);
    name.title = job.source;
    const badge = document.createElement("span");
    badge.className = `badge ${job.status}`;
    badge.textContent = statusLabel(job.status);
    const side = document.createElement("span");
    side.className = "job-side";
    if (ACTIVE_STATUSES.has(job.status) && job.finishes_in_seconds !== null) {
      const eta = document.createElement("span");
      eta.className = "muted small";
      eta.textContent = t("job.readyIn", { eta: formatEta(job.finishes_in_seconds) });
      eta.title =
        job.status === "queued"
          ? t("job.queuedTitle", { eta: formatEta(job.remaining_seconds) })
          : t("job.runningTitle");
      side.appendChild(eta);
    }
    side.appendChild(badge);
    head.append(name, side);
    item.appendChild(head);

    // What the job denoises with. A dropdown moved by an arrow key or a scroll
    // wheel looks exactly like a run with the usual settings until you listen.
    const meta = document.createElement("div");
    meta.className = "job-meta small";
    const requested = (job.settings && job.settings.denoise) || "auto";
    const result = job.result || {};
    if (result.denoise_used) {
      meta.textContent = t("job.denoise", { mode: result.denoise_used });
      if (result.denoise_filter) meta.title = result.denoise_filter;
      if (result.denoise_requested && result.denoise_requested !== result.denoise_used) {
        const fallback = document.createElement("span");
        fallback.className = "warn";
        fallback.textContent = t("job.denoiseFallback", { requested: result.denoise_requested });
        meta.appendChild(fallback);
      }
    } else {
      meta.textContent = t("job.denoise", { mode: requested });
    }
    item.appendChild(meta);

    const bar = document.createElement("div");
    bar.className = "progress mini";
    const fill = document.createElement("div");
    fill.style.width = `${Math.round((job.overall || 0) * 100)}%`;
    if (job.status === "done") fill.classList.add("done");
    if (job.status === "error" || job.status === "cancelled") fill.classList.add("error");
    bar.appendChild(fill);
    item.appendChild(bar);

    const actions = document.createElement("div");
    actions.className = "job-actions";
    if (ACTIVE_STATUSES.has(job.status)) {
      actions.appendChild(
        button(t("job.cancel"), "secondary tiny", async (event) => {
          event.stopPropagation();
          await api(`/api/jobs/${job.id}/cancel`, { method: "POST" });
          await refreshJobs();
        })
      );
    }
    if (job.live && ACTIVE_STATUSES.has(job.status)) {
      actions.appendChild(
        button(t("job.watch"), "tiny", (event) => {
          event.stopPropagation();
          playJob(job);
        })
      );
    } else if (ACTIVE_STATUSES.has(job.status) && job.settings && job.settings.live_dir) {
      // Shown before it works, so the feature can be found: on a long lecture
      // analysis and the silence pass take minutes before the render begins.
      const pending = button(t("job.watch"), "secondary tiny", () => {});
      pending.disabled = true;
      pending.title = t("job.watchPending");
      actions.appendChild(pending);
      const hint = document.createElement("span");
      hint.className = "muted small";
      hint.textContent = t("job.watchHint");
      actions.appendChild(hint);
    }
    if (job.status === "done" && job.result && job.result.output) {
      const output = job.result.output;
      actions.appendChild(
        button(t("job.watch"), "tiny", (event) => {
          event.stopPropagation();
          playJob(job);
        })
      );
      if (state.schema.allow_open) {
        actions.appendChild(
          button(t("job.open"), "tiny", (event) => {
            event.stopPropagation();
            openPath(output, false);
          })
        );
        actions.appendChild(
          button(t("job.folder"), "secondary tiny", (event) => {
            event.stopPropagation();
            openPath(output, true);
          })
        );
      }
      const link = document.createElement("a");
      link.className = "tiny-link";
      link.href = `/api/file?path=${encodeURIComponent(output)}`;
      link.textContent = t("job.download");
      link.addEventListener("click", (event) => event.stopPropagation());
      actions.appendChild(link);
    }
    if (job.status === "error" && job.error) {
      const why = document.createElement("span");
      why.className = "small err";
      why.textContent = job.error;
      actions.appendChild(why);
    }
    item.appendChild(actions);

    item.addEventListener("click", () => watchJob(job.id));
    list.appendChild(item);
  }
}

function button(text, className, handler) {
  const node = document.createElement("button");
  node.type = "button";
  node.className = className;
  node.textContent = text;
  node.addEventListener("click", handler);
  return node;
}

async function openPath(path, reveal) {
  try {
    await api("/api/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, reveal }),
    });
  } catch (error) {
    appendLog(t("err.open", { error: error.message }), true);
  }
}

// ------------------------------------------------------------------- player

function stopPlayer() {
  const video = el("video");
  if (state.player.hls) {
    state.player.hls.destroy();
    state.player.hls = null;
  }
  video.removeAttribute("src");
  video.load();
  state.player = { jobId: null, mode: null, hls: null };
  el("player").classList.add("hidden");
}

function playJob(job) {
  const video = el("video");
  el("player").classList.remove("hidden");
  if (state.player.hls) {
    state.player.hls.destroy();
    state.player.hls = null;
  }

  if (job.status === "done" && job.result && job.result.output) {
    state.player = { jobId: job.id, mode: "file", hls: null };
    video.src = `/api/file?path=${encodeURIComponent(job.result.output)}`;
    el("player-note").textContent = t("player.done", { name: baseName(job.result.output) });
    video.play().catch(() => {});
    return;
  }

  const playlist = `/api/jobs/${job.id}/live/index.m3u8`;
  el("player-note").textContent = t("player.live");
  if (window.Hls && window.Hls.isSupported()) {
    // startPosition 0: an event playlist would otherwise open at its live edge,
    // which for a render running at 10x is nowhere near the beginning.
    const hls = new window.Hls({ startPosition: 0 });
    hls.loadSource(playlist);
    hls.attachMedia(video);
    hls.on(window.Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
    state.player = { jobId: job.id, mode: "live", hls };
  } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = playlist;
    video.play().catch(() => {});
    state.player = { jobId: job.id, mode: "live", hls: null };
  } else {
    el("player-note").textContent = t("player.noHls");
  }
}

function swapToFinishedFile(job) {
  // Keep the viewer where they were: the playlist and the file share a timeline.
  const video = el("video");
  const at = video.currentTime;
  const wasPlaying = !video.paused;
  playJob(job);
  const resume = () => {
    video.removeEventListener("loadedmetadata", resume);
    if (at > 0 && at < (video.duration || Infinity)) video.currentTime = at;
    if (wasPlaying) video.play().catch(() => {});
  };
  video.addEventListener("loadedmetadata", resume);
}

async function refreshJobs() {
  try {
    const data = await api("/api/jobs");
    const watched = state.player.jobId
      ? data.jobs.find((job) => job.id === state.player.jobId)
      : null;
    if (watched && state.player.mode === "live" && watched.status === "done") {
      swapToFinishedFile(watched);
    }
    state.jobs = data.jobs;
    renderJobs();
    renderQueueSummary(data.queue);
    // Results land in folders the picker may not have listed yet.
    if (state.jobs.some((job) => job.status === "done")) {
      const known = new Set(state.files.map((file) => file.path));
      const fresh = state.jobs.some(
        (job) => job.result && job.result.output && !known.has(job.result.output)
      );
      if (fresh) await loadFiles();
    }
  } catch (error) {
    /* the poller keeps trying */
  }
}

// --------------------------------------------------------------- watched job

function appendLog(text, isError) {
  const log = el("log");
  const line = document.createElement("span");
  if (isError) line.className = "err";
  line.textContent = `${text}\n`;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function setProgress(overall) {
  const percent = Math.round((overall || 0) * 100);
  el("bar").style.width = `${percent}%`;
  el("percent").textContent = `${percent}%`;
}

function renderAnalysis(fields) {
  const box = el("analysis");
  box.classList.remove("hidden");
  box.textContent = "";
  const rows = [
    [
      t("an.speech"),
      t("an.speechValue", {
        median: fields.speech_lufs_median.toFixed(1),
        quiet: fields.speech_lufs.toFixed(1),
      }),
    ],
    [
      t("an.floor"),
      t("an.floorValue", {
        floor: fields.noise_floor_db.toFixed(1),
        snr: fields.snr_db.toFixed(1),
      }),
    ],
    [
      t("an.peak"),
      t("an.peakValue", {
        peak: fields.true_peak_db.toFixed(1),
        headroom: fields.headroom_db.toFixed(1),
      }),
    ],
    [
      t("an.dynamics"),
      t("an.dynamicsValue", {
        lra: fields.lra.toFixed(1),
        imbalance: fields.channel_imbalance_db.toFixed(1),
      }),
    ],
  ];
  for (const [label, value] of rows) {
    const row = document.createElement("div");
    row.textContent = `${label}: ${value}`;
    box.appendChild(row);
  }
  for (const hint of fields.hints || []) {
    const row = document.createElement("div");
    row.className = "hint";
    // The core sends a key and the numbers behind it, and its own English
    // sentence as a fallback for a hint this page has no wording for.
    row.textContent = `→ ${I18N[currentLang()][`hint.${hint.key}`] ? t(`hint.${hint.key}`, hint) : hint.text}`;
    box.appendChild(row);
  }
}

function renderResult(fields) {
  const box = el("result");
  box.classList.remove("hidden");
  box.className = "result ok";
  const size = fields.output_size ? ` · ${formatSize(fields.output_size)}` : "";
  box.textContent = [
    t("res.done", { output: fields.output, size }),
    t("res.encoder", { encoder: fields.encoder }),
    t("res.duration", {
      out: formatDuration(fields.output_duration),
      in: formatDuration(fields.input_duration),
    }),
    t("res.render", {
      seconds: Number(fields.render_seconds).toFixed(0),
      realtime: Number(fields.realtime).toFixed(2),
    }),
    t("res.threshold", { threshold: fields.silence_threshold }),
  ].join("\n");
}

function watchJob(jobId) {
  if (state.stream) {
    state.stream.close();
    state.stream = null;
  }
  state.watching = jobId;
  el("watch").classList.remove("hidden");
  el("log").textContent = "";
  el("analysis").classList.add("hidden");
  el("result").classList.add("hidden");
  el("bar").className = "";
  setProgress(0);
  el("phase").textContent = "—";
  renderJobs();

  const stream = new EventSource(`/api/jobs/${jobId}/events`);
  state.stream = stream;
  stream.onmessage = (message) => {
    const event = JSON.parse(message.data);
    switch (event.type) {
      case "line":
        appendLog(event.text, event.error);
        break;
      case "phase":
        el("phase").textContent = t(`phase.${event.phase}`);
        setProgress(event.overall);
        break;
      case "progress":
        setProgress(event.overall);
        break;
      case "analysis":
        renderAnalysis(event);
        break;
      case "result":
        renderResult(event);
        break;
      case "status":
        if (event.status === "done") {
          el("phase").textContent = t("phase.done");
          el("bar").className = "done";
          setProgress(1);
        } else if (event.status === "cancelled") {
          el("phase").textContent = t("phase.cancelled");
          el("bar").className = "error";
        } else if (event.status === "error") {
          el("phase").textContent = t("phase.error");
          el("bar").className = "error";
        }
        refreshJobs();
        break;
      default:
        break;
    }
  };
  stream.onerror = () => {
    // The server closes the stream once a job is finished; not a failure.
    stream.close();
    if (state.stream === stream) state.stream = null;
  };
}

async function convert() {
  const targets = batchTargets();
  if (!targets.length) return;
  const button = el("convert");
  button.disabled = true;
  const batch = targets.length > 1;
  const settings = collectSettings();
  const dir = el("set-output-dir").value;
  if (batch) delete settings.output;

  let first = null;
  let failed = 0;
  for (const target of targets) {
    const payload = { source: target.path, settings };
    // In a batch the folder travels separately and the server names each file.
    if (batch && dir) payload.output_dir = dir;
    try {
      const job = await api("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!first) first = job.id;
    } catch (error) {
      failed += 1;
      el("watch").classList.remove("hidden");
      appendLog(`${baseName(target.path)}: ${error.message}`, true);
    }
  }

  await refreshJobs();
  if (first && !failed) watchJob(first);
  state.checked.clear();
  state.primary = null;
  renderFiles();
  // Queueing more runs is the point: the button comes straight back.
  refreshPrimary();
}

// ---------------------------------------------------------------- language

function applyLanguage() {
  applyStaticText();
  for (const button of document.querySelectorAll("#lang-switch button")) {
    button.classList.toggle("chosen", button.dataset.lang === currentLang());
  }
  // Everything the page drew itself has to be drawn again in the new language.
  renderQualityLevels(true);
  renderModels();
  syncDenoiseHelp();
  syncQualityHint();
  renderDirs();
  renderFiles();
  refreshPrimary();
  renderJobs();
  refreshJobs();
}

// ------------------------------------------------------------------------ init

// Every element the script expects to find. A page served from cache can be
// older than the script that runs on it, and "el(...) is null" says nothing
// useful about that.
const REQUIRED_ELEMENTS = [
  "source-dir-path",
  "source-dir-tools",
  "drop-note",
  "set-arnndn-mix",
  "set-quality",
  "set-encoder",
  "file-list",
  "folder-dialog",
  "video",
  "player",
  "job-list",
  "convert",
];

function checkPageMatchesScript() {
  const missing = REQUIRED_ELEMENTS.filter((id) => !el(id));
  if (missing.length) {
    throw new Error(t("err.stalePage", { missing: missing.join(", ") }));
  }
}

async function init() {
  checkPageMatchesScript();
  setLanguage(currentLang());
  applyStaticText();
  state.schema = await api("/api/schema");
  for (const group of state.schema.groups) {
    for (const field of group.fields) {
      state.defaults[field.dest] = field.default;
      if (field.dest === "denoise") state.defaults.__denoise_choices = field.choices;
    }
  }
  applyDefaults();
  buildAdvanced();
  await Promise.all([loadFiles(), loadDirs()]);
  restoreFormState();
  await refreshJobs();

  el("file-filter").addEventListener("input", renderFiles);
  for (const button of document.querySelectorAll("#lang-switch button")) {
    button.addEventListener("click", () => {
      setLanguage(button.dataset.lang);
      applyLanguage();
    });
  }
  el("change-source-dir").addEventListener("click", openFolderBrowser);
  el("recent-source-dirs").addEventListener("change", (event) => {
    const path = event.target.value;
    event.target.value = "";
    chooseFolder(path);
  });
  el("folder-up").addEventListener("click", () => {
    if (state.browsing && state.browsing.parent) navigateFolder(state.browsing.parent);
  });
  el("folder-home").addEventListener("click", () => {
    navigateFolder(state.browsing ? state.browsing.home : "");
  });
  el("folder-go").addEventListener("click", () =>
    navigateFolder(el("folder-path").value.trim())
  );
  el("folder-path").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      navigateFolder(el("folder-path").value.trim());
    }
  });
  el("folder-choose").addEventListener("click", () => {
    if (state.browsing) chooseFolder(state.browsing.path);
  });
  el("folder-cancel").addEventListener("click", () => el("folder-dialog").close());
  el("player-close").addEventListener("click", stopPlayer);
  el("hide-processed").addEventListener("change", () => {
    // Hidden rows must not stay queued from a previous state of the filter.
    const shown = new Set(visibleFiles().map((file) => file.path));
    for (const path of [...state.checked]) {
      if (!shown.has(path)) state.checked.delete(path);
    }
    renderFiles();
    refreshPrimary();
  });
  updateConvertButton();
  el("convert").addEventListener("click", convert);
  el("set-denoise").addEventListener("change", syncDenoiseHelp);
  el("set-arnndn-mix").addEventListener("input", syncArnndnMix);
  el("set-quality").addEventListener("change", syncQualityHint);
  el("set-speed").addEventListener("input", syncQualityHint);
  el("set-encoder").addEventListener("change", syncQualityHint);
  el("fetch-model").addEventListener("click", () =>
    fetchModels([state.schema.default_model])
  );
  el("fetch-all-models").addEventListener("click", () => fetchModels("all"));
  el("set-silence-auto").addEventListener("change", syncSilenceControls);
  el("set-silence-bias").addEventListener("input", syncSilenceControls);
  el("reset-settings").addEventListener("click", resetFormState);
  for (const container of ["settings-panel", "source-panel"]) {
    for (const name of ["change", "input"]) {
      el(container).addEventListener(name, saveFormState);
    }
  }
  el("check-all").addEventListener("click", () => {
    for (const file of visibleFiles()) state.checked.add(file.path);
    renderFiles();
    refreshPrimary();
  });
  el("check-none").addEventListener("click", () => {
    state.checked.clear();
    state.primary = null;
    renderFiles();
    refreshPrimary();
  });
  el("open-output-dir").addEventListener("click", () => {
    const dir = el("set-output-dir").value;
    const target = dir || (state.selected ? dirName(state.selected.path) : "");
    if (target) openPath(target, false);
  });
  if (!state.schema.allow_open) el("open-output-dir").classList.add("hidden");

  const zone = el("dropzone");
  zone.addEventListener("click", () => el("file-filter").focus());
  for (const name of ["dragenter", "dragover"]) {
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.add("hot");
    });
  }
  for (const name of ["dragleave", "drop"]) {
    zone.addEventListener(name, () => zone.classList.remove("hot"));
  }
  zone.addEventListener("drop", async (event) => {
    event.preventDefault();
    const file = event.dataTransfer.files[0];
    if (!file) return;
    try {
      await handleDrop(file);
    } catch (error) {
      dropNote(t("drop.failed", { error: error.message }), true);
    }
  });
  window.addEventListener("dragover", (event) => event.preventDefault());
  window.addEventListener("drop", (event) => event.preventDefault());

  applyLanguage();
  state.poller = setInterval(refreshJobs, JOB_POLL_MS);
}

init().catch((error) => {
  document.body.insertAdjacentHTML(
    "afterbegin",
    `<p style="color:#e8735e;padding:16px">${t("err.startup", { error: error.message })}</p>`
  );
});
