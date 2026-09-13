"use strict";

// Knobs shown in the hand-built basic block; everything else is generated from
// the parser schema so the form never restates a default the CLI already owns.
const BASIC_DESTS = new Set([
  "speed",
  "target_lufs",
  "denoise",
  "silence_threshold",
  "start",
  "limit",
  "mono",
  "output",
]);
// The preview sweep is a separate flow, not part of this screen.
const SKIPPED_GROUPS = new Set(["preview sweep"]);

const state = {
  schema: null,
  defaults: {},
  files: [],
  selected: null,
  jobId: null,
  stream: null,
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
  for (const choice of state.defaults.__denoise_choices || []) {
    const option = document.createElement("option");
    option.value = choice;
    option.textContent = choice;
    denoise.appendChild(option);
  }
  denoise.value = d.denoise;
  const auto = String(d.silence_threshold).toLowerCase() === "auto";
  el("set-silence-auto").checked = auto;
  el("set-silence-threshold").disabled = auto;
  el("set-silence-threshold").value = auto ? "" : d.silence_threshold;
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
  if (el("set-mono").checked) settings.mono = true;

  const threshold = el("set-silence-auto").checked
    ? "auto"
    : el("set-silence-threshold").value.trim();
  put("silence_threshold", threshold);

  const start = el("set-start").value;
  if (start !== "") put("start", Number(start));
  const limit = el("set-limit").value;
  if (limit !== "") settings.limit = Number(limit);
  const output = el("set-output").value.trim();
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
  // Overwriting is the expected behaviour when a name is chosen deliberately.
  settings.force = true;
  return settings;
}

// ------------------------------------------------------------------ file picker

function renderFiles() {
  const filter = el("file-filter").value.trim().toLowerCase();
  const list = el("file-list");
  list.textContent = "";
  const matches = state.files.filter(
    (file) => !filter || file.name.toLowerCase().includes(filter)
  );
  if (!matches.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = state.files.length ? "Ничего не найдено" : "В доступных папках нет медиафайлов";
    list.appendChild(empty);
    return;
  }
  for (const file of matches) {
    const item = document.createElement("li");
    if (state.selected && state.selected.path === file.path) item.className = "active";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = file.relative || file.name;
    name.title = file.path;
    const size = document.createElement("span");
    size.className = "muted small";
    size.textContent = formatSize(file.size);
    item.append(name, size);
    item.addEventListener("click", () => selectFile(file));
    list.appendChild(item);
  }
}

async function selectFile(file) {
  state.selected = file;
  renderFiles();
  const box = el("selected");
  box.classList.remove("hidden");
  el("selected-name").textContent = file.name;
  el("selected-meta").textContent = `${formatSize(file.size)} · ${file.path}`;
  el("convert").disabled = false;
  try {
    const info = await api(`/api/probe?path=${encodeURIComponent(file.path)}`);
    const parts = [formatSize(file.size)];
    if (info.duration) parts.push(formatDuration(info.duration));
    if (!info.has_audio) parts.push("без аудио!");
    if (!info.has_video) parts.push("без видео!");
    el("selected-meta").textContent = `${parts.join(" · ")} · ${file.path}`;
    el("set-output").placeholder = info.default_output;
  } catch (error) {
    el("selected-meta").textContent = `${formatSize(file.size)} · ${error.message}`;
  }
}

async function loadFiles() {
  const data = await api("/api/files");
  state.files = data.files;
  renderFiles();
}

async function handleDrop(fileHandle) {
  const match = state.files.find(
    (file) => file.name === fileHandle.name && file.size === fileHandle.size
  );
  if (match) {
    await selectFile(match);
    return;
  }
  if (!state.schema.allow_upload) {
    appendLog(`Файл ${fileHandle.name} не найден в доступных папках, а загрузка отключена`, true);
    return;
  }
  appendLog(`Файл ${fileHandle.name} вне доступных папок, загружаю...`);
  const uploaded = await api(
    `/api/upload?name=${encodeURIComponent(fileHandle.name)}`,
    { method: "POST", body: fileHandle }
  );
  appendLog(`Загружено: ${uploaded.path}`);
  await loadFiles();
  const match2 = state.files.find((file) => file.path === uploaded.path);
  if (match2) await selectFile(match2);
}

// ------------------------------------------------------------------------- run

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
    ["Речь", `${fields.speech_lufs_median.toFixed(1)} LUFS (тихие места ${fields.speech_lufs.toFixed(1)})`],
    ["Шумовой пол", `${fields.noise_floor_db.toFixed(1)} дБ, SNR ${fields.snr_db.toFixed(1)} дБ`],
    ["Пик", `${fields.true_peak_db.toFixed(1)} dBFS, запас ${fields.headroom_db.toFixed(1)} дБ`],
    ["Динамика", `LRA до ${fields.lra.toFixed(1)} LU, каналы ${fields.channel_imbalance_db.toFixed(1)} дБ`],
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

function finishRun(status, message) {
  el("cancel").classList.add("hidden");
  el("convert").disabled = !state.selected;
  el("phase").textContent =
    status === "done" ? "Готово" : status === "cancelled" ? "Отменено" : "Ошибка";
  const bar = el("bar");
  bar.classList.remove("done", "error");
  if (status === "done") {
    bar.classList.add("done");
    setProgress(1);
  } else if (status === "error") {
    bar.classList.add("error");
  }
  if (message) {
    const box = el("result");
    box.classList.remove("hidden");
    box.className = `result ${status === "done" ? "ok" : "err"}`;
    box.textContent = message;
  }
  if (state.stream) {
    state.stream.close();
    state.stream = null;
  }
}

function listen(jobId) {
  if (state.stream) state.stream.close();
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
        if (event.status === "done") finishRun("done");
        else if (event.status === "cancelled") finishRun("cancelled", "Задача отменена");
        else if (event.status === "error") finishRun("error");
        break;
      default:
        break;
    }
  };
  stream.onerror = () => {
    // The server closes the stream when the job ends; that is not a failure.
    stream.close();
    if (state.stream === stream) state.stream = null;
  };
}

async function convert() {
  if (!state.selected) return;
  el("log").textContent = "";
  el("analysis").classList.add("hidden");
  el("result").classList.add("hidden");
  el("bar").classList.remove("done", "error");
  setProgress(0);
  el("phase").textContent = "В очереди";
  el("convert").disabled = true;
  el("cancel").classList.remove("hidden");

  try {
    const job = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: state.selected.path, settings: collectSettings() }),
    });
    state.jobId = job.id;
    listen(job.id);
  } catch (error) {
    appendLog(error.message, true);
    finishRun("error", error.message);
  }
}

async function cancel() {
  if (!state.jobId) return;
  el("cancel").disabled = true;
  try {
    await api(`/api/jobs/${state.jobId}/cancel`, { method: "POST" });
  } catch (error) {
    appendLog(error.message, true);
  } finally {
    el("cancel").disabled = false;
  }
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
  await loadFiles();

  el("file-filter").addEventListener("input", renderFiles);
  el("convert").addEventListener("click", convert);
  el("cancel").addEventListener("click", cancel);
  el("set-silence-auto").addEventListener("change", (event) => {
    el("set-silence-threshold").disabled = event.target.checked;
  });

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
      appendLog(error.message, true);
    }
  });
  window.addEventListener("dragover", (event) => event.preventDefault());
  window.addEventListener("drop", (event) => event.preventDefault());
}

init().catch((error) => {
  document.body.insertAdjacentHTML(
    "afterbegin",
    `<p style="color:#e8735e;padding:16px">Не удалось запустить интерфейс: ${error.message}</p>`
  );
});
