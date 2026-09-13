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
  "if_exists",
  "start",
  "limit",
  "mono",
  "output",
]);
// The preview sweep is a separate flow, not part of this screen.
const SKIPPED_GROUPS = new Set(["preview sweep"]);
const ACTIVE_STATUSES = new Set(["queued", "running"]);
const SETTINGS_KEY = "lecturecut.settings.v1";
// Deliberately not remembered: they belong to one particular file, and silently
// reusing them would quietly process 60 seconds of the next lecture.
const NEVER_REMEMBERED = new Set(["set-output-name", "set-start", "set-limit"]);
const JOB_POLL_MS = 1500;

const STATUS_LABELS = {
  queued: "в очереди",
  running: "выполняется",
  done: "готово",
  error: "ошибка",
  cancelled: "отменено",
};

const state = {
  schema: null,
  defaults: {},
  files: [],
  dirs: [],
  selected: null,
  primary: null,
  checked: new Set(),
  jobs: [],
  watching: null,
  stream: null,
  poller: null,
};

const el = (id) => document.getElementById(id);

async function api(path, options) {
  const response = await fetch(path, options);
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
  renderModels();
  syncDenoiseHelp();
  const auto = String(d.silence_threshold).toLowerCase() === "auto";
  el("set-silence-auto").checked = auto;
  el("set-silence-threshold").disabled = auto;
  el("set-silence-threshold").value = auto ? "" : d.silence_threshold;
  el("set-silence-bias").value = d.silence_bias ?? 0;
  syncSilenceControls();
}

function syncDenoiseHelp() {
  const mode = el("set-denoise").value;
  const help = (state.schema.denoise_help || {})[mode] || "";
  const models = state.schema.models || [];
  // arnndn is the best of the three and the only one that needs a file, so the
  // model picker appears exactly when it is about to be used.
  const needsModel = mode === "arnndn" || (mode === "auto" && models.length > 0);
  el("model-label").classList.toggle("hidden", !needsModel);
  let note = help;
  if (mode === "arnndn" && !models.length) {
    note = `${help}. Модель не найдена — ${state.schema.model_hint}`;
  }
  el("denoise-help").textContent = note;
  el("denoise-help").classList.toggle("warn", mode === "arnndn" && !models.length);
}

function renderModels() {
  const select = el("set-arnndn-model");
  const previous = select.value;
  select.textContent = "";
  const models = state.schema.models || [];
  if (!models.length) {
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "моделей не найдено";
    select.appendChild(empty);
    select.disabled = true;
  } else {
    select.disabled = false;
    for (const model of models) {
      const option = document.createElement("option");
      option.value = model.path;
      option.textContent = model.name;
      option.title = model.path;
      select.appendChild(option);
    }
    select.value = previous || models[0].path;
  }
  el("model-hint").textContent = models.length ? "" : state.schema.model_hint;
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
  auto.textContent = "рядом с источником";
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
  const model = el("set-arnndn-model").value;
  if (model && !el("model-label").classList.contains("hidden")) {
    settings.arnndn_model = model;
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
    empty.textContent = state.files.length
      ? "Ничего не найдено"
      : "В доступных папках нет медиафайлов";
    if (state.files.length && hidden > 0) {
      empty.textContent = "Все подходящие файлы уже обработаны";
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
      done.textContent = "обработано";
      done.title = "У файла есть метка LectureCut либо имя результата";
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
    chosen.length > 1 ? `${chosen.length} файла(ов) выбрано` : file.name;
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
    targets.length > 1 ? `Конвертировать (${targets.length})` : "Конвертировать";
  el("checked-count").textContent = state.checked.size
    ? `выбрано: ${state.checked.size}`
    : "";
  // One explicit name cannot serve a batch; the core names each output instead.
  const name = el("set-output-name");
  name.disabled = targets.length > 1;
  name.placeholder =
    targets.length > 1
      ? "имена задаются по каждому источнику"
      : state.selected && state.selected.defaultOutput
        ? baseName(state.selected.defaultOutput)
        : "как у источника";
}

async function describeFile(file) {
  try {
    const info = await api(`/api/probe?path=${encodeURIComponent(file.path)}`);
    file.defaultOutput = info.default_output;
    if (state.selected !== file) return;
    const parts = [formatSize(file.size)];
    if (info.duration) parts.push(formatDuration(info.duration));
    if (!info.has_audio) parts.push("без аудио!");
    if (!info.has_video) parts.push("без видео!");
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
  renderFiles();
}

async function loadDirs() {
  const data = await api("/api/dirs");
  state.dirs = data.dirs;
  renderDirs();
}

async function handleDrop(fileHandle) {
  const match = state.files.find(
    (file) => file.name === fileHandle.name && file.size === fileHandle.size
  );
  if (match) {
    if (!state.checked.has(match.path)) toggleFile(match);
    return;
  }
  if (!state.schema.allow_upload) {
    appendLog(`Файл ${fileHandle.name} не найден в доступных папках, а загрузка отключена`, true);
    return;
  }
  appendLog(`Файл ${fileHandle.name} вне доступных папок, загружаю...`);
  const uploaded = await api(`/api/upload?name=${encodeURIComponent(fileHandle.name)}`, {
    method: "POST",
    body: fileHandle,
  });
  appendLog(`Загружено: ${uploaded.path}`);
  await loadFiles();
  const match2 = state.files.find((file) => file.path === uploaded.path);
  if (match2 && !state.checked.has(match2.path)) toggleFile(match2);
}

// ----------------------------------------------------------------- job queue

function statusLabel(status) {
  return STATUS_LABELS[status] || status;
}

function renderJobs() {
  const list = el("job-list");
  list.textContent = "";
  if (!state.jobs.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = "Очередь пуста";
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
    head.append(name, badge);
    item.appendChild(head);

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
        button("Отмена", "secondary tiny", async (event) => {
          event.stopPropagation();
          await api(`/api/jobs/${job.id}/cancel`, { method: "POST" });
          await refreshJobs();
        })
      );
    }
    if (job.status === "done" && job.result && job.result.output) {
      const output = job.result.output;
      if (state.schema.allow_open) {
        actions.appendChild(
          button("Открыть", "tiny", (event) => {
            event.stopPropagation();
            openPath(output, false);
          })
        );
        actions.appendChild(
          button("Папка", "secondary tiny", (event) => {
            event.stopPropagation();
            openPath(output, true);
          })
        );
      }
      const link = document.createElement("a");
      link.className = "tiny-link";
      link.href = `/api/file?path=${encodeURIComponent(output)}`;
      link.textContent = "Скачать";
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
    appendLog(`Не удалось открыть: ${error.message}`, true);
  }
}

async function refreshJobs() {
  try {
    const data = await api("/api/jobs");
    state.jobs = data.jobs;
    renderJobs();
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
      "Речь",
      `${fields.speech_lufs_median.toFixed(1)} LUFS (тихие места ${fields.speech_lufs.toFixed(1)})`,
    ],
    ["Шумовой пол", `${fields.noise_floor_db.toFixed(1)} дБ, SNR ${fields.snr_db.toFixed(1)} дБ`],
    ["Пик", `${fields.true_peak_db.toFixed(1)} dBFS, запас ${fields.headroom_db.toFixed(1)} дБ`],
    [
      "Динамика",
      `LRA до ${fields.lra.toFixed(1)} LU, каналы ${fields.channel_imbalance_db.toFixed(1)} дБ`,
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
    row.textContent = `→ ${hint}`;
    box.appendChild(row);
  }
}

function renderResult(fields) {
  const box = el("result");
  box.classList.remove("hidden");
  box.className = "result ok";
  box.textContent = [
    `Готово: ${fields.output}`,
    `Кодировщик: ${fields.encoder}`,
    `Длительность: ${formatDuration(fields.output_duration)} из ${formatDuration(fields.input_duration)}`,
    `Рендер: ${Number(fields.render_seconds).toFixed(0)} с (${Number(fields.realtime).toFixed(2)}× realtime)`,
    `Порог тишины: ${fields.silence_threshold}`,
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
        el("phase").textContent = event.label || event.phase;
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
          el("phase").textContent = "Готово";
          el("bar").className = "done";
          setProgress(1);
        } else if (event.status === "cancelled") {
          el("phase").textContent = "Отменено";
          el("bar").className = "error";
        } else if (event.status === "error") {
          el("phase").textContent = "Ошибка";
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

// ------------------------------------------------------------------------ init

async function init() {
  state.schema = await api("/api/schema");
  el("roots").textContent = `Доступные папки: ${state.schema.roots.join(", ")}`;
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
      el("watch").classList.remove("hidden");
      appendLog(error.message, true);
    }
  });
  window.addEventListener("dragover", (event) => event.preventDefault());
  window.addEventListener("drop", (event) => event.preventDefault());

  state.poller = setInterval(refreshJobs, JOB_POLL_MS);
}

init().catch((error) => {
  document.body.insertAdjacentHTML(
    "afterbegin",
    `<p style="color:#e8735e;padding:16px">Не удалось запустить интерфейс: ${error.message}</p>`
  );
});
