"use strict";

// Interface text in both languages. The code, the command line and the log the
// pipeline writes stay English; this covers what the page says for itself.
const I18N = {
  ru: {
    "panel.source": "Источник",
    "panel.settings": "Настройки",
    "panel.queue": "Очередь",
    "panel.progress": "Ход выполнения",

    "src.folder": "Папка с лекциями",
    "src.change": "Сменить…",
    "src.recentTitle": "Недавние папки",
    "src.dropHint":
      "браузер не передаёт путь, поэтому файл ищется по имени и размеру среди известных папок",
    "src.filter": "Фильтр по имени",
    "src.checkAll": "Выбрать все",
    "src.checkNone": "Снять выбор",
    "src.hideProcessed": "Скрывать уже обработанные",

    "drop.idle": "Перетащите файл сюда или выберите из списка",
    "drop.selected": "Выбран {name}",
    "drop.searching": "Ищу {name} в известных папках…",
    "drop.selectedFrom": "Выбран {name} из {dir}",
    "drop.uploadDisabled": "{name} нет в известных папках, а загрузка отключена",
    "drop.confirm":
      "{name} нет в известных папках.\n\nСкопировать его сюда через браузер ({size})?\nБыстрее выбрать его папку кнопкой «Сменить…».",
    "drop.pickFolder": "Выберите папку с этим файлом кнопкой «Сменить…»",
    "drop.uploading": "Загружаю {name} ({size})…",
    "drop.uploaded": "Загружен {name}",
    "drop.failed": "Не получилось: {error}",

    "set.speed": "Скорость",
    "set.targetLufs": "Целевая громкость, LUFS",
    "set.denoise": "Шумодав",
    "set.model": "Модель arnndn",
    "set.download": "Скачать",
    "set.all": "Все",
    "set.strength": "Сила arnndn",
    "set.strengthHint": "меньше — тихая речь уцелеет, но и шума останется больше",
    "set.silence": "Порог тишины",
    "set.auto": "авто",
    "set.bias": "Поправка к авто, дБ",
    "set.biasHint": "больше — режет больше пауз",
    "set.start": "С секунды (необязательно)",
    "set.limit": "Длительность, с (необязательно)",
    "set.limitPlaceholder": "весь файл",
    "set.mono": "Свести в моно",
    "set.quality": "Сжатие видео",
    "set.encoder": "Кодировщик",
    "set.outputDir": "Папка результата",
    "set.open": "Открыть",
    "set.openTitle": "Открыть папку",
    "set.outputName": "Имя файла",
    "set.outputNamePlaceholder": "как у источника",
    "set.advanced": "Дополнительно",
    "set.convert": "Конвертировать",
    "set.convertN": "Конвертировать ({n})",
    "set.reset": "Сбросить",
    "set.queueHint": "выбирайте несколько файлов — они выполнятся по очереди",

    "enc.gpu": "видеокарта (быстро)",
    "enc.cpu": "процессор (файл вдвое меньше, рендер вдвое дольше)",
    "enc.hevc": "видеокарта, HEVC (тоже вдвое меньше, но без просмотра на ходу)",

    "q.max": "максимальное качество",
    "q.high": "высокое",
    "q.normal": "обычное",
    "q.compact": "компактное",
    "q.min": "минимальный размер",
    "q.hint":
      "≈ {perHour} ГБ на час записи{file} — прикидка по лекции с доской, зависит от съёмки",
    "q.hintFile": " · этот файл не больше {size}",

    "den.auto": "arnndn, если указана модель, иначе afftdn",
    "den.afftdn": "спектральный, быстрый; сила подбирается по замерам",
    "den.anlmdn": "нелокальное среднее, медленнее, иногда чище на широкополосном шуме",
    "den.arnndn": "нейросеть для речи, лучший из трёх, нужна модель",
    "den.none": "не трогать шум",
    "den.noModel": "{help}. Модели ещё нет — нажмите «Скачать»",
    "den.modelsNone": "моделей нет",
    "den.fetchMore": "Ещё {n} ({kb} КБ)",
    "den.downloading": "Скачиваю…",
    "den.downloadFailed": "Не удалось скачать: {error}",
    "den.recommended": "рекомендуется для лекций",
    "den.modelHint":
      "Модели по ~300 КБ, скачиваются по кнопке в кэш {dir}. Хеши зашиты, так что загрузка проверяется",

    "signal.speech": "речь",
    "signal.voice": "речь и смех",
    "signal.general": "любой звук",
    "noise.recording": "шум записи",
    "noise.general": "любой шум",

    "files.noMatch": "Ничего не найдено",
    "files.none": "В этой папке нет медиафайлов",
    "files.allProcessed": "Все подходящие файлы уже обработаны",
    "files.processed": "обработано",
    "files.processedTitle": "У файла есть метка LectureCut либо имя результата",
    "files.selectedN": "выбрано файлов: {n}",
    "files.checked": "выбрано: {n}",
    "files.batchNames": "имена задаются по каждому источнику",
    "files.noAudio": "без аудио!",
    "files.noVideo": "без видео!",

    "folder.title": "Папка с лекциями",
    "folder.up": "На уровень выше",
    "folder.home": "Домой",
    "folder.go": "Перейти",
    "folder.choose": "Выбрать эту папку",
    "folder.cancel": "Отмена",
    "folder.mediaHere": "Здесь медиафайлов: {n}",
    "folder.mediaNone": "Здесь медиафайлов нет — возможно, они во вложенных папках",
    "folder.noSub": "Вложенных папок нет",
    "folder.recent": "Недавние…",
    "folder.recentNone": "недавних нет",
    "folder.failed": "Не удалось выбрать папку: {error}",
    "dir.nextToSource": "рядом с источником",

    "queue.empty": "Очередь пуста",
    "queue.pending": "в работе и в очереди: {n}",
    "queue.remaining": "осталось {approx}{eta}",
    "queue.atLeast": "не меньше ",
    "queue.finishBy": "закончится к {time}",
    "queue.unknown": "длительность неизвестна для {n}",
    "queue.learning": " (оценка уточнится после первой готовой задачи)",

    "eta.lessMinute": "меньше минуты",
    "eta.minutes": "{n} мин",
    "eta.hours": "{h} ч",
    "eta.hoursMinutes": "{h} ч {m} мин",

    "job.readyIn": "готово через ≈ {eta}",
    "job.queuedTitle": "С учётом задач перед ней. Сама она займёт ≈ {eta}",
    "job.runningTitle": "По текущему темпу этого этапа и скорости прошлых задач",
    "job.denoise": "шумодав: {mode}",
    "job.denoiseFallback": " — вместо {requested}: тот портил речь",
    "job.cancel": "Отмена",
    "job.watch": "Смотреть",
    "job.watchPending": "Появится, когда начнётся рендер — после анализа звука и поиска тишины",
    "job.watchHint": "просмотр — с началом рендера",
    "job.open": "Открыть",
    "job.folder": "Папка",
    "job.download": "Скачать",

    "status.queued": "в очереди",
    "status.running": "выполняется",
    "status.done": "готово",
    "status.error": "ошибка",
    "status.cancelled": "отменено",

    "player.close": "Закрыть",
    "player.done": "Готово: {name}",
    "player.live": "Идёт конвертация — смотреть можно с начала, перемотка до отрендеренного места",
    "player.noHls": "Браузер не умеет проигрывать HLS",

    "phase.measure": "Замеряю звук",
    "phase.calibrate": "Подбираю настройки",
    "phase.silence": "Ищу тишину",
    "phase.render": "Рендер",
    "phase.done": "Готово",
    "phase.cancelled": "Отменено",
    "phase.error": "Ошибка",

    "an.speech": "Речь",
    "an.speechValue": "{median} LUFS (тихие места {quiet})",
    "an.floor": "Шумовой пол",
    "an.floorValue": "{floor} дБ, SNR {snr} дБ",
    "an.peak": "Пик",
    "an.peakValue": "{peak} dBFS, запас {headroom} дБ",
    "an.dynamics": "Динамика",
    "an.dynamicsValue": "LRA до {lra} LU, каналы {imbalance} дБ",

    "hint.mono": "Каналы расходятся на {imbalance} дБ; «Свести в моно» даст более чистый голос",
    "hint.declick": "Запас по пикам всего {headroom} дБ: где-то уже есть щелчок на всю шкалу, поможет --declick",
    "hint.arnndn": "SNR {snr} дБ; стоит попробовать шумодав arnndn с моделью",

    "res.done": "Готово: {output}{size}",
    "res.encoder": "Кодировщик: {encoder}",
    "res.duration": "Длительность: {out} из {in}",
    "res.render": "Рендер: {seconds} с ({realtime}× realtime)",
    "res.threshold": "Порог тишины: {threshold}",

    "err.stalePage":
      "страница из кэша браузера старее скрипта — обновите её с Ctrl+Shift+R (не хватает: {missing})",
    "err.startup": "Не удалось запустить интерфейс: {error}",
    "err.open": "Не удалось открыть: {error}",
  },

  en: {
    "panel.source": "Source",
    "panel.settings": "Settings",
    "panel.queue": "Queue",
    "panel.progress": "Progress",

    "src.folder": "Lecture folder",
    "src.change": "Change…",
    "src.recentTitle": "Recent folders",
    "src.dropHint":
      "a browser withholds the path, so a dropped file is matched by name and size among the known folders",
    "src.filter": "Filter by name",
    "src.checkAll": "Select all",
    "src.checkNone": "Clear selection",
    "src.hideProcessed": "Hide already processed",

    "drop.idle": "Drop a file here, or pick one from the list",
    "drop.selected": "Selected {name}",
    "drop.searching": "Looking for {name} in the known folders…",
    "drop.selectedFrom": "Selected {name} from {dir}",
    "drop.uploadDisabled": "{name} is not in the known folders, and uploads are off",
    "drop.confirm":
      "{name} is not in the known folders.\n\nCopy it here through the browser ({size})?\nPicking its folder with “Change…” is quicker.",
    "drop.pickFolder": "Use “Change…” to pick the folder this file is in",
    "drop.uploading": "Uploading {name} ({size})…",
    "drop.uploaded": "Uploaded {name}",
    "drop.failed": "Did not work: {error}",

    "set.speed": "Speed",
    "set.targetLufs": "Target loudness, LUFS",
    "set.denoise": "Denoiser",
    "set.model": "arnndn model",
    "set.download": "Download",
    "set.all": "All",
    "set.strength": "arnndn strength",
    "set.strengthHint": "lower keeps quiet speech, and keeps more of the noise",
    "set.silence": "Silence threshold",
    "set.auto": "auto",
    "set.bias": "Adjust the automatic value, dB",
    "set.biasHint": "higher cuts more pauses",
    "set.start": "Start at second (optional)",
    "set.limit": "Duration, s (optional)",
    "set.limitPlaceholder": "whole file",
    "set.mono": "Downmix to mono",
    "set.quality": "Video compression",
    "set.encoder": "Encoder",
    "set.outputDir": "Output folder",
    "set.open": "Open",
    "set.openTitle": "Open the folder",
    "set.outputName": "File name",
    "set.outputNamePlaceholder": "same as the source",
    "set.advanced": "Advanced",
    "set.convert": "Convert",
    "set.convertN": "Convert ({n})",
    "set.reset": "Reset",
    "set.queueHint": "pick several files — they run one after another",

    "enc.gpu": "graphics card (fast)",
    "enc.cpu": "processor (half the file, twice the render time)",
    "enc.hevc": "graphics card, HEVC (half the file, no live preview)",

    "q.max": "best quality",
    "q.high": "high",
    "q.normal": "normal",
    "q.compact": "compact",
    "q.min": "smallest file",
    "q.hint":
      "≈ {perHour} GB per hour of recording{file} — measured on a whiteboard lecture, depends on the footage",
    "q.hintFile": " · this file no more than {size}",

    "den.auto": "arnndn when a model is given, otherwise afftdn",
    "den.afftdn": "spectral gate, fast; its strength comes from the measurements",
    "den.anlmdn": "non-local means, slower, sometimes cleaner on broadband hiss",
    "den.arnndn": "recurrent network trained on speech, the best of these; needs a model",
    "den.none": "leave the noise alone",
    "den.noModel": "{help}. No model yet — press “Download”",
    "den.modelsNone": "no models",
    "den.fetchMore": "{n} more ({kb} KB)",
    "den.downloading": "Downloading…",
    "den.downloadFailed": "Could not download: {error}",
    "den.recommended": "recommended for lectures",
    "den.modelHint":
      "Models are ~300 KB each and land in {dir}; their digests are pinned, so a download is verified",

    "signal.speech": "speech",
    "signal.voice": "speech and laughter",
    "signal.general": "any audio",
    "noise.recording": "recording noise",
    "noise.general": "any noise",

    "files.noMatch": "Nothing found",
    "files.none": "No media in this folder",
    "files.allProcessed": "Every file here has been processed already",
    "files.processed": "processed",
    "files.processedTitle": "It carries a LectureCut tag, or the name of a result",
    "files.selectedN": "{n} files selected",
    "files.checked": "selected: {n}",
    "files.batchNames": "names come from each source",
    "files.noAudio": "no audio!",
    "files.noVideo": "no video!",

    "folder.title": "Lecture folder",
    "folder.up": "Up one level",
    "folder.home": "Home",
    "folder.go": "Go",
    "folder.choose": "Use this folder",
    "folder.cancel": "Cancel",
    "folder.mediaHere": "Media files here: {n}",
    "folder.mediaNone": "No media here — they may be in a subfolder",
    "folder.noSub": "No subfolders",
    "folder.recent": "Recent…",
    "folder.recentNone": "none recent",
    "folder.failed": "Could not use that folder: {error}",
    "dir.nextToSource": "next to the source",

    "queue.empty": "The queue is empty",
    "queue.pending": "running and queued: {n}",
    "queue.remaining": "{approx}{eta} left",
    "queue.atLeast": "at least ",
    "queue.finishBy": "done by {time}",
    "queue.unknown": "length unknown for {n}",
    "queue.learning": " (the estimate sharpens after the first finished job)",

    "eta.lessMinute": "less than a minute",
    "eta.minutes": "{n} min",
    "eta.hours": "{h} h",
    "eta.hoursMinutes": "{h} h {m} min",

    "job.readyIn": "ready in ≈ {eta}",
    "job.queuedTitle": "Including everything ahead of it; on its own ≈ {eta}",
    "job.runningTitle": "From this stage's own pace and how fast previous jobs ran",
    "job.denoise": "denoiser: {mode}",
    "job.denoiseFallback": " — instead of {requested}, which was harming the speech",
    "job.cancel": "Cancel",
    "job.watch": "Watch",
    "job.watchPending": "Appears once rendering starts — after the audio analysis and the silence pass",
    "job.watchHint": "viewing starts with the render",
    "job.open": "Open",
    "job.folder": "Folder",
    "job.download": "Download",

    "status.queued": "queued",
    "status.running": "running",
    "status.done": "done",
    "status.error": "error",
    "status.cancelled": "cancelled",

    "player.close": "Close",
    "player.done": "Done: {name}",
    "player.live": "Still converting — watch from the start; seeking works up to the rendered point",
    "player.noHls": "This browser cannot play HLS",

    "phase.measure": "Measuring audio",
    "phase.calibrate": "Calibrating settings",
    "phase.silence": "Detecting silence",
    "phase.render": "Rendering",
    "phase.done": "Done",
    "phase.cancelled": "Cancelled",
    "phase.error": "Error",

    "an.speech": "Speech",
    "an.speechValue": "{median} LUFS (quiet passages {quiet})",
    "an.floor": "Noise floor",
    "an.floorValue": "{floor} dB, SNR {snr} dB",
    "an.peak": "Peak",
    "an.peakValue": "{peak} dBFS, headroom {headroom} dB",
    "an.dynamics": "Dynamics",
    "an.dynamicsValue": "LRA up to {lra} LU, channels {imbalance} dB",

    "hint.mono": "Channels differ by {imbalance} dB; “Downmix to mono” trades stereo for a cleaner voice",
    "hint.declick": "Only {headroom} dB of headroom: a transient already sits near full scale, --declick may help",
    "hint.arnndn": "SNR is {snr} dB; the arnndn denoiser with a model is worth a try",

    "res.done": "Done: {output}{size}",
    "res.encoder": "Encoder: {encoder}",
    "res.duration": "Duration: {out} of {in}",
    "res.render": "Render: {seconds} s ({realtime}× realtime)",
    "res.threshold": "Silence threshold: {threshold}",

    "err.stalePage":
      "the page is an older cached copy than the script — reload it with Ctrl+Shift+R (missing: {missing})",
    "err.startup": "Could not start the interface: {error}",
    "err.open": "Could not open: {error}",
  },
};

const LANG_KEY = "lecturecut.lang";
let LANG = pickLanguage();

function pickLanguage() {
  try {
    const stored = localStorage.getItem(LANG_KEY);
    if (stored && I18N[stored]) return stored;
  } catch (error) {
    /* a private window has no storage; the browser's own language will do */
  }
  return (navigator.language || "en").toLowerCase().startsWith("ru") ? "ru" : "en";
}

function currentLang() {
  return LANG;
}

function setLanguage(lang) {
  if (!I18N[lang]) return;
  LANG = lang;
  try {
    localStorage.setItem(LANG_KEY, lang);
  } catch (error) {
    /* not worth failing over */
  }
  document.documentElement.lang = lang;
}

function t(key, params) {
  // English is the fallback: a key missing from a translation shows the original
  // rather than the key itself.
  const template = I18N[LANG][key] ?? I18N.en[key] ?? key;
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (whole, name) =>
    name in params ? String(params[name]) : whole
  );
}

function applyStaticText(root) {
  const scope = root || document;
  for (const node of scope.querySelectorAll("[data-i18n]")) {
    node.textContent = t(node.dataset.i18n);
  }
  for (const node of scope.querySelectorAll("[data-i18n-placeholder]")) {
    node.placeholder = t(node.dataset.i18nPlaceholder);
  }
  for (const node of scope.querySelectorAll("[data-i18n-title]")) {
    node.title = t(node.dataset.i18nTitle);
  }
}
