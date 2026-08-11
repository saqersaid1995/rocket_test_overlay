const form = document.querySelector("#renderForm");
const videoInput = document.querySelector("#videoInput");
const videoInput2 = document.querySelector("#videoInput2");
const videoInput3 = document.querySelector("#videoInput3");
const dataInput = document.querySelector("#dataInput");
const logoInput = document.querySelector("#logoInput");
const video = document.querySelector("#previewVideo");
const video2 = document.querySelector("#previewVideo2");
const video3 = document.querySelector("#previewVideo3");
const stage = document.querySelector("#editorStage");
const elementHost = document.querySelector("#sceneElements");
const chartSvg = document.querySelector("#chartPreview");
const toast = document.querySelector("#toast");
const broadcastOverlay = document.querySelector("#broadcastOverlay");
const broadcastRasterOverlay = document.querySelector("#broadcastRasterOverlay");
const templatePackageInput = document.querySelector("#templatePackageInput");
const installedTemplatesHost = document.querySelector("#installedTemplates");
const selectedTemplateIdInput = document.querySelector("#selectedTemplateId");
const selectedTemplateVersionInput = document.querySelector("#selectedTemplateVersion");
const selectedTemplateSha256Input = document.querySelector("#selectedTemplateSha256");

document.body.classList.add("broadcast-mode");

const broadcastThemes = {
  launch: {
    name: "A · Launch Broadcast",
    description: "مطابق للتصميم الموجود في rocket_overlay_broadcast.py.",
    features: ["ARC GAUGES", "MISSION CLOCK", "LIVE TRACE"],
  },
  mission_control: {
    name: "B · Mission Control",
    description: "مطابق للتصميم الموجود في rocket_overlay_broadcast (1).py.",
    features: ["BAR METERS", "PHASE CHECKLIST", "STRIP CHART"],
  },
  stellar_console: {
    name: "3 · Stellar Broadcast Console",
    description: "مطابق للتصميم الأصلي في rocket_overlay_broadcast-3).py.",
    features: ["VERTICAL PRESSURE TAPE", "MISSION CONSOLE", "PHASE SEQUENCER"],
  },
};

let installedTemplatePackages = [];
let selectedTemplatePackage = null;
let templatesRequestSequence = 0;

function templatePackageKey(template) {
  if (!template) return "";
  return `${template.id || template.template_id || ""}@${template.version || template.template_version || ""}`;
}

function templatePackageName(template) {
  const name = template?.name;
  if (typeof name === "string" && name.trim()) return name.trim();
  if (name && typeof name === "object") {
    return String(name.ar || name.en || Object.values(name)[0] || "").trim();
  }
  return template?.id || template?.template_id || "قالب ROTPL";
}

function normalizeTemplatePackage(template) {
  if (!template || typeof template !== "object") return null;
  const id = String(template.id || template.template_id || "").trim();
  const version = String(template.version || template.template_version || "").trim();
  if (!id || !version) return null;
  const validation = template.validation && typeof template.validation === "object"
    ? template.validation : {};
  const status = String(template.status || "").toLowerCase();
  const blockedReasons = Array.isArray(validation.blocked_reasons)
    ? validation.blocked_reasons.slice()
    : (Array.isArray(template.blocked_reasons) ? template.blocked_reasons.slice() : []);
  const blocked = template.blocked === true || validation.activatable === false ||
    blockedReasons.length > 0 || ["blocked", "rejected", "invalid"].includes(status);
  const validationErrors = Array.isArray(validation.errors)
    ? validation.errors.slice()
    : (Array.isArray(template.errors) ? template.errors.slice() : []);
  if (blocked && !validationErrors.length && (template.block_reason || template.error)) {
    validationErrors.push(template.block_reason || template.error);
  }
  if (blocked && blockedReasons.length) validationErrors.push(...blockedReasons);
  return {
    ...template,
    id,
    version,
    sha256: String(template.sha256 || "").trim(),
    blocked,
    validation: {
      ...validation,
      valid: !blocked && (validation.valid === true || template.valid === true),
      errors: validationErrors,
      warnings: Array.isArray(validation.warnings) ? validation.warnings : [],
    },
  };
}

function templateValidationMessage(issue) {
  if (typeof issue === "string") return issue;
  if (!issue || typeof issue !== "object") return String(issue || "خطأ غير معروف");
  const location = issue.path || issue.file || issue.field || "";
  const message = issue.message || issue.error || issue.code || "مشكلة في الحزمة";
  return location ? `${location}: ${message}` : String(message);
}

function selectedTemplatePayload() {
  if (!selectedTemplatePackage) return {};
  return {
    template_id: selectedTemplatePackage.id,
    template_version: selectedTemplatePackage.version,
    ...(selectedTemplatePackage.sha256 ? { sha256: selectedTemplatePackage.sha256 } : {}),
  };
}

function setTemplateFeedback(message = "", tone = "info", issues = []) {
  const host = document.querySelector("#templatePackageFeedback");
  if (!host) return;
  host.replaceChildren();
  host.dataset.tone = tone;
  host.classList.toggle("hidden", !message && !issues.length);
  if (message) {
    const title = document.createElement("strong");
    title.textContent = message;
    host.append(title);
  }
  if (issues.length) {
    const list = document.createElement("ul");
    issues.forEach(issue => {
      const item = document.createElement("li");
      item.textContent = templateValidationMessage(issue);
      list.append(item);
    });
    host.append(list);
  }
}

function updateTemplateUploadProgress(value, message) {
  const progress = document.querySelector("#templateUploadProgress");
  const percent = clamp(Math.round(Number(value) || 0), 0, 100);
  progress?.classList.remove("hidden");
  progress?.setAttribute("aria-valuenow", String(percent));
  const percentElement = document.querySelector("#templateUploadPercent");
  const bar = document.querySelector("#templateUploadBar");
  const messageElement = document.querySelector("#templateUploadMessage");
  if (percentElement) percentElement.textContent = `${percent}%`;
  if (bar) bar.style.width = `${percent}%`;
  if (messageElement) messageElement.textContent = message;
}

function setSelectedTemplatePackage(template, announce = false) {
  selectedTemplatePackage = template ? normalizeTemplatePackage(template) : null;
  selectedTemplateIdInput.value = selectedTemplatePackage?.id || "";
  selectedTemplateVersionInput.value = selectedTemplatePackage?.version || "";
  selectedTemplateSha256Input.value = selectedTemplatePackage?.sha256 || "";
  document.querySelector(".broadcast-panel")?.classList.toggle(
    "custom-template-selected", Boolean(selectedTemplatePackage)
  );
  if (selectedTemplatePackage) {
    localStorage.setItem("rocket-overlay-template-package", templatePackageKey(selectedTemplatePackage));
    broadcastOverlay.dataset.templateId = selectedTemplatePackage.id;
    broadcastOverlay.dataset.templateVersion = selectedTemplatePackage.version;
    const name = templatePackageName(selectedTemplatePackage);
    document.querySelector("#broadcastEditionName").textContent = name.toUpperCase();
    document.querySelector("#broadcastThemeName").textContent = name;
    document.querySelector("#broadcastThemeDescription").textContent =
      `حزمة ROTPL إصدار ${selectedTemplatePackage.version} — تستخدم في المعاينة والتصدير.`;
    document.querySelector("#broadcastFeatures").innerHTML =
      `<span>ROTPL</span><span>v${escapeHtml(selectedTemplatePackage.version)}</span><span>1920 × 1080</span>`;
  } else {
    localStorage.setItem("rocket-overlay-template-package", "__builtin__");
    delete broadcastOverlay.dataset.templateId;
    delete broadcastOverlay.dataset.templateVersion;
  }
  renderInstalledTemplatePackages();
  invalidateBroadcastRasterWork();
  clearBroadcastRasterPreview();
  renderBroadcastPreview();
  if (announce && selectedTemplatePackage) {
    notify(`تم تطبيق قالب ${templatePackageName(selectedTemplatePackage)} إصدار ${selectedTemplatePackage.version}.`);
  }
}

function templateStatusLabel(template, isSelected) {
  if (isSelected) return "قيد الاستخدام";
  if (template.blocked) return "محظور — راجع الأخطاء";
  if (!template.validation.valid) return "غير صالح";
  if (template.validation.warnings.length) return "جاهز مع تحذيرات";
  return "جاهز";
}

function appendTemplateIssueSummary(card, template) {
  const errors = template.validation.errors;
  const warnings = template.validation.warnings;
  if (!errors.length && !warnings.length) return;
  const details = document.createElement("details");
  details.className = "template-validation-details";
  const summary = document.createElement("summary");
  summary.textContent = errors.length
    ? `${errors.length} أخطاء تمنع التفعيل`
    : `${warnings.length} تحذيرات`;
  details.append(summary);
  const list = document.createElement("ul");
  [...errors, ...warnings].slice(0, 8).forEach(issue => {
    const item = document.createElement("li");
    item.textContent = templateValidationMessage(issue);
    list.append(item);
  });
  details.append(list);
  card.append(details);
}

function renderInstalledTemplatePackages() {
  if (!installedTemplatesHost) return;
  installedTemplatesHost.replaceChildren();
  installedTemplatesHost.setAttribute("aria-busy", "false");
  if (!installedTemplatePackages.length) {
    const empty = document.createElement("div");
    empty.className = "template-list-empty";
    const title = document.createElement("strong");
    title.textContent = "لا توجد قوالب ROTPL مثبتة";
    const help = document.createElement("span");
    help.textContent = "ارفع stellar-kinetics.rotpl ليظهر هنا بعد التحقق.";
    empty.append(title, help);
    installedTemplatesHost.append(empty);
    return;
  }

  installedTemplatePackages.forEach(template => {
    const isSelected = templatePackageKey(template) === templatePackageKey(selectedTemplatePackage);
    const card = document.createElement("article");
    card.className = "installed-template-card";
    card.dataset.tone = isSelected ? "active" : (template.validation.valid ? "ready" : "error");

    const visual = document.createElement("div");
    visual.className = "installed-template-visual";
    if (template.thumbnail_url) {
      const thumbnail = document.createElement("img");
      thumbnail.src = template.thumbnail_url;
      thumbnail.alt = "";
      thumbnail.loading = "lazy";
      visual.append(thumbnail);
    } else {
      visual.textContent = "ROTPL";
    }

    const copy = document.createElement("div");
    copy.className = "installed-template-copy";
    const title = document.createElement("strong");
    title.textContent = templatePackageName(template);
    const meta = document.createElement("span");
    const bindings = Number(template.bindings_count);
    meta.textContent = `v${template.version}${Number.isFinite(bindings) ? ` · ${bindings} متغيرًا` : ""}`;
    const status = document.createElement("small");
    status.textContent = templateStatusLabel(template, isSelected);
    copy.append(title, meta, status);

    const action = document.createElement("button");
    action.type = "button";
    action.className = "template-use-button";
    action.disabled = isSelected || !template.validation.valid;
    action.textContent = isSelected ? "مفعّل" : (template.validation.valid ? "استخدام" : "مرفوض");
    action.setAttribute(
      "aria-label",
      `${action.textContent} ${templatePackageName(template)} إصدار ${template.version}`
    );
    action.addEventListener("click", () => activateTemplatePackage(template));

    card.append(visual, copy, action);
    appendTemplateIssueSummary(card, template);
    installedTemplatesHost.append(card);
  });
}

async function loadTemplatePackages({ preferKey = "" } = {}) {
  if (!installedTemplatesHost) return;
  const sequence = ++templatesRequestSequence;
  installedTemplatesHost.setAttribute("aria-busy", "true");
  try {
    const response = await fetch("/api/templates", { headers: { "Accept": "application/json" } });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "تعذر تحميل القوالب المثبتة.");
    if (sequence !== templatesRequestSequence) return;
    installedTemplatePackages = (Array.isArray(payload.templates) ? payload.templates : [])
      .map(normalizeTemplatePackage).filter(Boolean);
    const storedChoice = localStorage.getItem("rocket-overlay-template-package");
    const activeKey = templatePackageKey(payload.active);
    const desiredKey = preferKey || (
      storedChoice === "__builtin__" ? "__builtin__" : (storedChoice || activeKey)
    );
    const desired = desiredKey === "__builtin__" ? null : installedTemplatePackages.find(item =>
      templatePackageKey(item) === desiredKey && item.validation.valid
    );
    if (desiredKey === "__builtin__") setSelectedTemplatePackage(null, false);
    else if (desired) setSelectedTemplatePackage(desired, false);
    else if (selectedTemplatePackage) {
      const retained = installedTemplatePackages.find(item =>
        templatePackageKey(item) === templatePackageKey(selectedTemplatePackage) && item.validation.valid
      );
      setSelectedTemplatePackage(retained || null, false);
    } else renderInstalledTemplatePackages();
    return true;
  } catch (error) {
    if (sequence !== templatesRequestSequence) return;
    installedTemplatesHost.setAttribute("aria-busy", "false");
    installedTemplatesHost.replaceChildren();
    const failed = document.createElement("div");
    failed.className = "template-list-empty error";
    failed.textContent = error.message;
    installedTemplatesHost.append(failed);
    setTemplateFeedback(error.message, "error");
    return false;
  }
}

async function activateTemplatePackage(template) {
  const normalized = normalizeTemplatePackage(template);
  if (!normalized?.validation.valid) return;
  installedTemplatesHost?.setAttribute("aria-busy", "true");
  setTemplateFeedback(`جارٍ تفعيل ${templatePackageName(normalized)}…`, "info");
  try {
    const response = await fetch(`/api/templates/${encodeURIComponent(normalized.id)}/activate`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ version: normalized.version }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "تعذر تفعيل القالب.");
    const active = normalizeTemplatePackage(payload.active || payload.template || normalized) || normalized;
    if (!active.validation.valid) active.validation = normalized.validation;
    setSelectedTemplatePackage({ ...normalized, ...active, validation: normalized.validation }, true);
    setTemplateFeedback(
      `تم تفعيل ${templatePackageName(normalized)} إصدار ${normalized.version} للمعاينة والتصدير.`,
      "success"
    );
  } catch (error) {
    setTemplateFeedback(error.message, "error");
    notify(error.message);
    renderInstalledTemplatePackages();
  } finally {
    installedTemplatesHost?.setAttribute("aria-busy", "false");
  }
}

function uploadTemplatePackage(file) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    const body = new FormData();
    body.append("template", file, file.name);
    request.open("POST", "/api/templates");
    request.setRequestHeader("Accept", "application/json");
    request.upload.addEventListener("progress", event => {
      if (!event.lengthComputable) return;
      const percent = Math.round(event.loaded / event.total * 100);
      updateTemplateUploadProgress(percent, percent < 100 ? "جارٍ رفع الحزمة…" : "جارٍ فحص الحزمة…");
    });
    request.addEventListener("load", () => {
      let payload = {};
      try { payload = JSON.parse(request.responseText || "{}"); } catch {}
      if (request.status >= 200 && request.status < 300) resolve(payload);
      else reject(Object.assign(
        new Error(payload.error || `تعذر رفع القالب (${request.status}).`),
        { issues: payload.validation?.errors || payload.errors || [] }
      ));
    });
    request.addEventListener("error", () => reject(new Error("انقطع الاتصال أثناء رفع القالب.")));
    request.addEventListener("abort", () => reject(new Error("تم إلغاء رفع القالب.")));
    request.send(body);
  });
}

async function handleTemplatePackageUpload() {
  const file = templatePackageInput?.files?.[0];
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".rotpl")) {
    setTemplateFeedback("الملف غير مدعوم. اختر حزمة تنتهي بالامتداد .rotpl.", "error");
    templatePackageInput.value = "";
    return;
  }
  const uploadLabel = document.querySelector("#templateUploadLabel");
  uploadLabel?.classList.add("busy");
  templatePackageInput.disabled = true;
  setTemplateFeedback("", "info");
  updateTemplateUploadProgress(0, `جارٍ تجهيز ${file.name}…`);
  try {
    const payload = await uploadTemplatePackage(file);
    const rawTemplate = payload.template || payload;
    const uploaded = normalizeTemplatePackage(rawTemplate);
    const validation = rawTemplate?.validation || payload.validation || {};
    updateTemplateUploadProgress(100, "اكتمل الرفع والفحص");
    if (validation.valid === true && validation.activatable !== false && !uploaded?.blocked) {
      setTemplateFeedback(
        "الحزمة سليمة وجاهزة. راجع التحذيرات ثم اضغط «استخدام» لتفعيلها.",
        validation.warnings?.length ? "warning" : "success",
        validation.warnings || []
      );
    } else if (validation.valid === true) {
      setTemplateFeedback(
        "الحزمة سليمة بنيويًا، لكن تفعيلها محظور حتى تُستكمل المتطلبات التالية:",
        "error",
        validation.blocked_reasons || uploaded?.validation?.errors || []
      );
    } else {
      setTemplateFeedback(
        "تم رفع الحزمة، لكنها لم تجتز الفحص ولن يمكن تفعيلها.",
        "error",
        validation.errors || []
      );
    }
    await loadTemplatePackages({ preferKey: "" });
  } catch (error) {
    updateTemplateUploadProgress(0, "فشل رفع القالب");
    setTemplateFeedback(error.message, "error", error.issues || []);
    notify(error.message);
  } finally {
    templatePackageInput.disabled = false;
    templatePackageInput.value = "";
    uploadLabel?.classList.remove("busy");
  }
}

function selectedBroadcastTheme() {
  return document.querySelector('input[name="broadcast_theme"]:checked')?.value || "launch";
}

function applyBroadcastTheme(theme, announce = false) {
  const selected = broadcastThemes[theme] ? theme : "launch";
  const details = broadcastThemes[selected];
  const themeChanged = broadcastOverlay.dataset.theme !== selected;
  const radio = document.querySelector(`input[name="broadcast_theme"][value="${selected}"]`);
  if (radio) radio.checked = true;
  broadcastOverlay.dataset.theme = selected;
  if (themeChanged) {
    invalidateBroadcastRasterWork();
    broadcastOverlay.classList.remove("raster-ready");
    broadcastOverlay.classList.toggle("raster-pending", Boolean(broadcastPreviewSessionId));
  }
  document.querySelector("#broadcastEditionName").textContent = details.name.toUpperCase();
  document.querySelector("#broadcastThemeName").textContent = details.name;
  document.querySelector("#broadcastThemeDescription").textContent = details.description;
  document.querySelector("#broadcastFeatures").innerHTML = details.features
    .map(feature => `<span>${feature}</span>`).join("");
  document.querySelector("#showBroadcastChart")?.closest("label")
    ?.classList.toggle("hidden", selected === "stellar_console");
  localStorage.setItem("rocket-overlay-broadcast-theme", selected);
  renderBroadcastPreview();
  if (announce) notify(`تم تطبيق ثيم ${details.name} على المعاينة والتصدير.`);
}

const names = {
  video: "الفيديو", chart: "المخطط", title: "العنوان", timecode: "الوقت والمزامنة",
  pressure_card: "بطاقة الضغط", thrust_card: "بطاقة الدفع", status: "حالة الاختبار",
  identity: "بيانات الاختبار", logo: "الشعار"
};
let scene = null;
let initialScene = null;
let selectedId = null;
let previewUrl = null;
let previewUrl2 = null;
let previewUrl3 = null;
let logoUrl = null;
let broadcastPreviewSessionId = null;
let broadcastRasterUrl = null;
let broadcastRasterTimer = null;
let broadcastRasterRequest = null;
let broadcastRasterSequence = 0;
let broadcastRasterGeneration = 0;
let broadcastRasterPendingTime = null;
let broadcastRasterLastStartedAt = 0;
let broadcastRasterDisplayedSequence = 0;
let videoFrameCallbackId = null;
let videoFrameLastUpdateAt = 0;
let telemetryRows = [];
let telemetryDiagnosticsData = null;
let runtimeTelemetry = null;
let telemetryZeroAuto = true;
let inspectRequest = null;
let inspectSequence = 0;
let lastRasterError = "";
let pollTimer = null;
let undoStack = [];
let redoStack = [];
let dragState = null;
let propertyEditing = false;
let cameraPanState = null;

function clone(value) { return JSON.parse(JSON.stringify(value)); }
function clamp(value, low, high) { return Math.max(low, Math.min(high, value)); }
function selected() { return scene?.elements.find(item => item.id === selectedId); }
function notify(message) {
  toast.textContent = message; toast.style.display = "block";
  window.setTimeout(() => toast.style.display = "none", 4200);
}
function fileName(input, id) {
  document.querySelector(id).textContent = input.files[0]?.name || "لم يتم اختيار ملف";
}
function snapshot() {
  if (!scene || propertyEditing) return;
  undoStack.push(JSON.stringify(scene));
  if (undoStack.length > 60) undoStack.shift();
  redoStack = [];
}
function restore(serialized) {
  scene = JSON.parse(serialized); selectedId = null; renderScene(); selectElement(null);
}
function applyEngineeringTheme(target) {
  target.designRevision = 8;
  target.background = "#05080C";
  const styles = {
    title:{x:.04,y:.05,width:.46,height:.09,color:"#FFFFFF",background:"#050B11",backgroundOpacity:0},
    timecode:{x:.745,y:.055,width:.115,height:.062,color:"#FFFFFF",background:"#050B11",backgroundOpacity:.58},
    chart:{x:.02,y:.805,width:.96,height:.165,background:"#040A10",backgroundOpacity:.93,pressureColor:"#5EDCFF",thrustColor:"#FFCB66",gridColor:"#718491",cursorColor:"#FFFFFF",fillOpacity:.10,lineWidth:4,borderRadius:5,chartType:"area",hudMode:true},
    pressure_card:{x:.71,y:.825,width:.125,height:.125,color:"#5EDCFF",background:"#040A10",backgroundOpacity:0},
    thrust_card:{x:.845,y:.825,width:.115,height:.125,color:"#FFCB66",background:"#040A10",backgroundOpacity:0},
    status:{x:.875,y:.055,width:.105,height:.062,color:"#B9F3CC",background:"#050B11",backgroundOpacity:.58},
    identity:{x:.04,y:.142,width:.46,height:.035,color:"#EDF3F6",background:"#050B11",backgroundOpacity:0},
    logo:{x:.91,y:.14,width:.07,height:.055}
  };
  target.elements.forEach(element=>Object.assign(element,styles[element.type]||{}));
  return target;
}

async function initialize() {
  const saved = localStorage.getItem("rocket-overlay-scene");
  try {
    const response = await fetch("/api/default-scene");
    initialScene = await response.json();
    scene = saved ? JSON.parse(saved) : clone(initialScene);
    if ((scene.designRevision || 0) < 8) {
      applyEngineeringTheme(scene);
      saveDraft();
    }
  } catch {
    notify("تعذر تحميل التصميم الافتراضي.");
    return;
  }
  renderScene();
  bindProperties();
}

videoInput.addEventListener("change", () => {
  fileName(videoInput, "#videoName");
  if (!videoInput.files[0]) return;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(videoInput.files[0]);
  const resolution = document.querySelector("#resolution");
  resolution.value = "source";
  configureResolutionOptions(0, 0);
  video.src = previewUrl; video.style.display = "block";
  stage.classList.add("has-source");
  document.querySelector("#emptyPreview").classList.add("hidden");
});
videoInput2.addEventListener("change", () => fileName(videoInput2, "#videoName2"));
videoInput3.addEventListener("change", () => fileName(videoInput3, "#videoName3"));
function setCameraSource(input, target, urlKey) {
  const oldUrl = urlKey === 2 ? previewUrl2 : previewUrl3;
  if (oldUrl) URL.revokeObjectURL(oldUrl);
  const nextUrl = input.files[0] ? URL.createObjectURL(input.files[0]) : null;
  if (urlKey === 2) previewUrl2 = nextUrl; else previewUrl3 = nextUrl;
  if (nextUrl) target.src = nextUrl; else target.removeAttribute("src");
  updateCameraPreview();
}
videoInput2.addEventListener("change", () => setCameraSource(videoInput2, video2, 2));
videoInput3.addEventListener("change", () => setCameraSource(videoInput3, video3, 3));
document.querySelector("#cameraMode").addEventListener("change", updateCameraPreview);
document.querySelector("#cameraPreviewMode").addEventListener("change", updateCameraPreview);

function cameraSourceTime(cameraNumber) {
  const primaryIgnition = Number(form.elements.ignition_video_s.value || 0);
  const cameraIgnition = Number(form.elements[`camera_${cameraNumber}_ignition_s`].value || 0);
  return video.currentTime - primaryIgnition + cameraIgnition;
}
function syncSecondaryCameras() {
  [[video2,2],[video3,3]].forEach(([camera,number]) => {
    if (!camera.src) return;
    const target = cameraSourceTime(number);
    if (target >= 0 && Number.isFinite(camera.duration) && Math.abs(camera.currentTime-target) > .2)
      camera.currentTime = Math.min(target, camera.duration);
  });
}
function updateCameraPreview() {
  const cameras = [video, video2, video3];
  cameras.forEach(camera => {
    camera.style.cssText = "display:none;position:absolute;object-fit:cover;";
  });
  if (!video.src) return;
  const previewChoice = document.querySelector("#cameraPreviewMode").value;
  if (previewChoice !== "layout") {
    const chosen = cameras[Number(previewChoice) - 1];
    if (!chosen?.src) {
      notify(`اختر ملف الكاميرا ${previewChoice} أولًا.`);
      document.querySelector("#cameraPreviewMode").value = "layout";
    } else {
      chosen.style.cssText = "display:block;position:absolute;object-fit:cover;inset:0;width:100%;height:100%;";
      applyCameraFocus(chosen, Number(previewChoice));
      syncSecondaryCameras();
      return;
    }
  }
  const mode = form.elements.camera_mode.value;
  const active = cameras.filter(camera => camera.src);
  if (mode === "split" && active.length > 1) {
    active.forEach((camera,index) => {
      camera.style.cssText = `display:block;position:absolute;object-fit:cover;height:100%;width:${100/active.length}%;left:${index*100/active.length}%;top:0;`;
      applyCameraFocus(camera, cameras.indexOf(camera) + 1);
    });
  } else if (mode === "pip" && active.length > 1) {
    video.style.cssText = "display:block;position:absolute;object-fit:cover;inset:0;width:100%;height:100%;";
    applyCameraFocus(video, 1);
    active.slice(1).forEach((camera,index) => {
      camera.style.cssText = `display:block;position:absolute;object-fit:cover;width:30%;height:30%;right:2%;top:${2+index*32}%;z-index:8;border:2px solid #eef6fb;`;
      applyCameraFocus(camera, cameras.indexOf(camera) + 1);
    });
  } else if (mode === "switch" && active.length > 1) {
    const ignition = Number(form.elements.ignition_video_s.value || 0);
    const delay = Number(form.elements.camera_3_switch_delay_s.value || 2);
    const chosen = active.length > 2 && video.currentTime >= ignition + delay
      ? video3 : (video.currentTime >= ignition ? video2 : video);
    const visible = chosen.src ? chosen : video;
    visible.style.cssText = "display:block;position:absolute;object-fit:cover;inset:0;width:100%;height:100%;";
    applyCameraFocus(visible, cameras.indexOf(visible) + 1);
  } else {
    video.style.cssText = "display:block;position:absolute;object-fit:cover;inset:0;width:100%;height:100%;";
    applyCameraFocus(video, 1);
  }
  syncSecondaryCameras();
}
function applyCameraFocus(camera, number) {
  const prefix = number === 1 ? "" : `camera_${number}_`;
  const x = Number(form.elements[`${prefix}crop_x`]?.value ?? .5);
  const y = Number(form.elements[`${prefix}crop_y`]?.value ?? .5);
  camera.style.objectPosition = `${x * 100}% ${y * 100}%`;
}
function cameraAtPointer(event) {
  const explicit = document.querySelector("#cameraPreviewMode").value;
  if (explicit !== "layout") return Number(explicit);
  const mode = form.elements.camera_mode.value;
  const available = [video, video2, video3].filter(camera => camera.src);
  if (mode === "split" && available.length > 1) {
    const rect = stage.getBoundingClientRect();
    const cell = Math.min(
      available.length - 1,
      Math.max(0, Math.floor((event.clientX - rect.left) / rect.width * available.length))
    );
    return [video, video2, video3].indexOf(available[cell]) + 1;
  }
  return explicit === "layout" ? 1 : Number(explicit);
}
stage.addEventListener("pointerdown", event => {
  if (!document.querySelector("#cameraDragMode").checked) return;
  const cameraNumber = cameraAtPointer(event);
  const prefix = cameraNumber === 1 ? "" : `camera_${cameraNumber}_`;
  cameraPanState = {
    pointerId:event.pointerId, cameraNumber,
    startX:event.clientX, startY:event.clientY,
    cropX:Number(form.elements[`${prefix}crop_x`].value),
    cropY:Number(form.elements[`${prefix}crop_y`].value)
  };
  stage.setPointerCapture(event.pointerId);
  event.preventDefault(); event.stopPropagation();
}, true);
stage.addEventListener("pointermove", event => {
  if (!cameraPanState || event.pointerId !== cameraPanState.pointerId) return;
  const rect = stage.getBoundingClientRect();
  const prefix = cameraPanState.cameraNumber === 1 ? "" : `camera_${cameraPanState.cameraNumber}_`;
  form.elements[`${prefix}crop_x`].value = clamp(
    cameraPanState.cropX - (event.clientX-cameraPanState.startX)/rect.width, 0, 1
  );
  form.elements[`${prefix}crop_y`].value = clamp(
    cameraPanState.cropY - (event.clientY-cameraPanState.startY)/rect.height, 0, 1
  );
  updateCameraPreview();
  event.preventDefault(); event.stopPropagation();
}, true);
stage.addEventListener("pointerup", event => {
  if (!cameraPanState || event.pointerId !== cameraPanState.pointerId) return;
  cameraPanState = null;
  stage.releasePointerCapture(event.pointerId);
  event.preventDefault(); event.stopPropagation();
}, true);
video.addEventListener("loadedmetadata", () => {
  document.querySelector("#duration").textContent = formatTime(video.duration);
  const resolution = document.querySelector("#resolution");
  resolution.value = "source";
  configureResolutionOptions(video.videoWidth, video.videoHeight);
  stage.style.aspectRatio = `${video.videoWidth}/${video.videoHeight}`;
  syncSourceQualityMode();
  updateIgnitionMarker(); updateCameraPreview(); renderPreview();
});
function updatePlaybackPreview() {
  if (!video.duration) return;
  document.querySelector("#timelineRange").value = video.currentTime / video.duration * 1000;
  updateCameraPreview(); renderPreview();
}

function stopVideoFrameLoop() {
  if (videoFrameCallbackId !== null && typeof video.cancelVideoFrameCallback === "function") {
    video.cancelVideoFrameCallback(videoFrameCallbackId);
  }
  videoFrameCallbackId = null;
}

function startVideoFrameLoop() {
  stopVideoFrameLoop();
  if (typeof video.requestVideoFrameCallback !== "function") return;
  const onFrame = now => {
    if (video.paused || video.ended) {
      videoFrameCallbackId = null;
      return;
    }
    // The exact raster queue is single-flight; 12 fps is enough for smooth
    // telemetry motion without rebuilding the browser fallback 120 times/s.
    if (now - videoFrameLastUpdateAt >= 80) {
      videoFrameLastUpdateAt = now;
      updatePlaybackPreview();
    }
    videoFrameCallbackId = video.requestVideoFrameCallback(onFrame);
  };
  videoFrameCallbackId = video.requestVideoFrameCallback(onFrame);
}

video.addEventListener("timeupdate", updatePlaybackPreview);
video.addEventListener("play", () => {
  document.querySelector("#playButton").textContent = "❚❚";
  syncSecondaryCameras();
  [video2, video3].forEach(camera => camera.src && camera.play().catch(()=>{}));
  invalidateBroadcastRasterWork();
  videoFrameLastUpdateAt = 0;
  startVideoFrameLoop();
  updatePlaybackPreview();
});
video.addEventListener("pause", () => {
  document.querySelector("#playButton").textContent = "▶";
  [video2, video3].forEach(camera => camera.pause());
  stopVideoFrameLoop();
  invalidateBroadcastRasterWork();
  updatePlaybackPreview();
});
video.addEventListener("ended", stopVideoFrameLoop);
video.addEventListener("seeking", invalidateBroadcastRasterWork);
video.addEventListener("seeked", updatePlaybackPreview);

logoInput.addEventListener("change", () => {
  fileName(logoInput, "#logoName");
  if (logoUrl) URL.revokeObjectURL(logoUrl);
  logoUrl = logoInput.files[0] ? URL.createObjectURL(logoInput.files[0]) : null;
  invalidateBroadcastRasterWork();
  renderScene();
  renderBroadcastPreview();
  void syncBroadcastPreviewLogo();
});
dataInput.addEventListener("change", inspectData);
["timeColumn", "pressureColumn", "thrustColumn"].forEach(id =>
  document.querySelector(`#${id}`).addEventListener("change", () => {
    invalidateBroadcastRasterWork();
    renderTelemetryDiagnostics();
    renderPreview();
  })
);
form.elements.sheet.addEventListener("change", () => {
  if (dataInput.files[0]) inspectData();
});
form.elements.telemetry_zero_s.addEventListener("input", () => {
  telemetryZeroAuto = form.elements.telemetry_zero_s.value === "";
  invalidateBroadcastRasterWork();
  renderTelemetryDiagnostics();
  renderPreview();
});
form.elements.time_scale.addEventListener("change", () => {
  invalidateBroadcastRasterWork();
  renderTelemetryDiagnostics();
  renderPreview();
});
form.elements.ignition_video_s.addEventListener("input", () => {
  invalidateBroadcastRasterWork();
  updateIgnitionMarker();
  renderPreview();
});
["camera_2_ignition_s","camera_3_ignition_s","camera_3_switch_delay_s"].forEach(name =>
  form.elements[name].addEventListener("input", updateCameraPreview)
);
[
  "crop_x","crop_y","crop_zoom",
  "camera_2_crop_x","camera_2_crop_y","camera_2_crop_zoom",
  "camera_3_crop_x","camera_3_crop_y","camera_3_crop_zoom"
].forEach(name => form.elements[name].addEventListener("input", updateCameraPreview));
function renderConfiguredPreview() {
  invalidateBroadcastRasterWork();
  renderPreview();
}
form.elements.title.addEventListener("input", renderConfiguredPreview);
form.elements.subtitle.addEventListener("input", renderConfiguredPreview);
form.elements.pressure_unit.addEventListener("input", renderConfiguredPreview);
form.elements.thrust_unit.addEventListener("input", renderConfiguredPreview);
["run_number", "motor_type", "propellant", "test_date", "organization_name",
 "oxidizer", "fuel", "ablative_material", "test_site", "coordinates_text",
 "footer_tagline", "camera_label", "capture_fps"].forEach(name =>
  form.elements[name].addEventListener("input", renderConfiguredPreview)
);
form.elements.pressure_limit.addEventListener("input", renderConfiguredPreview);
["broadcastAccent", "showBroadcastChart", "showBroadcastPhases"].forEach(id =>
  document.querySelector(`#${id}`).addEventListener("input", () => {
    invalidateBroadcastRasterWork();
    renderBroadcastPreview();
  })
);
document.querySelectorAll('input[name="broadcast_theme"]').forEach(input =>
  input.addEventListener("change", () => {
    setSelectedTemplatePackage(null, false);
    applyBroadcastTheme(input.value, true);
  })
);
templatePackageInput?.addEventListener("change", handleTemplatePackageUpload);
document.querySelector("#refreshTemplates")?.addEventListener("click", () => {
  setTemplateFeedback("جارٍ تحديث قائمة القوالب…", "info");
  void loadTemplatePackages().then(success => {
    if (success) setTemplateFeedback();
  });
});

async function inspectData() {
  fileName(dataInput, "#dataName");
  broadcastPreviewSessionId = null;
  telemetryDiagnosticsData = null;
  runtimeTelemetry = null;
  clearBroadcastRasterPreview();
  if (inspectRequest) inspectRequest.abort();
  if (!dataInput.files[0]) {
    renderTelemetryDiagnostics();
    return;
  }
  const controller = new AbortController();
  inspectRequest = controller;
  const sequence = ++inspectSequence;
  const body = new FormData();
  body.append("data", dataInput.files[0]);
  body.append("sheet", form.elements.sheet.value);
  try {
    const response = await fetch("/api/inspect", {
      method: "POST", body, signal: controller.signal,
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error);
    if (sequence !== inspectSequence) return;
    broadcastPreviewSessionId = payload.preview_id || null;
    telemetryRows = payload.series || [];
    telemetryDiagnosticsData = payload.telemetry_diagnostics || null;
    for (const kind of ["time", "pressure", "thrust"]) {
      const select = document.querySelector(`#${kind}Column`);
      const optional = kind === "thrust" ? '<option value="__none__">غير متوفر — ضغط فقط</option>' : "";
      select.innerHTML = optional + payload.columns.map(column =>
        `<option value="${escapeHtml(column)}">${escapeHtml(column)}</option>`).join("");
      select.value = payload.detected[kind] || (kind === "thrust" ? "__none__" : payload.columns[0]);
    }
    const suggestedZero = Number(payload.suggested_telemetry_zero_s);
    if (Number.isFinite(suggestedZero) &&
        (telemetryZeroAuto || form.elements.telemetry_zero_s.value === "")) {
      form.elements.telemetry_zero_s.value = String(Number(suggestedZero.toFixed(6)));
      telemetryZeroAuto = true;
    }
    renderTelemetryDiagnostics();
    if (broadcastPreviewSessionId && logoInput.files[0]) {
      await syncBroadcastPreviewLogo(false);
    }
    renderPreview();
    const thrustNotice = telemetryDiagnosticsData?.has_thrust
      ? "تم ربط الضغط والدفع."
      : "تم ربط الضغط؛ قناة الدفع غير موجودة وستظهر N/A.";
    notify(`تم تحليل القياسات واكتشاف نقطة الاشتعال تلقائيًا. ${thrustNotice}`);
  } catch (error) {
    if (error.name !== "AbortError") {
      telemetryDiagnosticsData = { status: "error", reason: error.message };
      renderTelemetryDiagnostics();
      notify(error.message);
    }
  } finally {
    if (inspectRequest === controller) inspectRequest = null;
  }
}

function formatTelemetryNumber(value, digits = 3) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits).replace(/\.?0+$/, "") : "—";
}

function renderTelemetryDiagnostics(runtimeError = "") {
  const panel = document.querySelector("#telemetryDiagnostics");
  if (!panel) return;
  if (!dataInput.files[0] && !runtimeError) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  const diagnostics = telemetryDiagnosticsData || {};
  const hasThrust = Boolean(form.elements.thrust_column.value) && form.elements.thrust_column.value !== "__none__";
  const status = runtimeError ? "error" : diagnostics.status;
  panel.dataset.tone = status === "error" ? "error" : (hasThrust ? "ready" : "warning");
  document.querySelector("#telemetryHealth").textContent = runtimeError
    ? `تعذر تحديث المعاينة: ${runtimeError}`
    : status === "ready"
      ? "تم ربط القياسات بنجاح"
      : "جارٍ تحليل وربط القياسات";
  const timeColumn = form.elements.time_column.value || diagnostics.time_column || "—";
  const pressureColumn = form.elements.pressure_column.value || diagnostics.pressure_column || "—";
  const thrustColumn = hasThrust ? form.elements.thrust_column.value : "N/A (غير موجود)";
  document.querySelector("#telemetryMapping").textContent =
    `TIME ← ${timeColumn}   |   PRESSURE ← ${pressureColumn}   |   THRUST ← ${thrustColumn}`;
  const sourceStart = formatTelemetryNumber(diagnostics.first_timestamp_s);
  const sourceEnd = formatTelemetryNumber(diagnostics.last_timestamp_s);
  const zero = formatTelemetryNumber(form.elements.telemetry_zero_s.value);
  const sourceLabel = diagnostics.first_timestamp_s == null
    ? `${diagnostics.row_count || telemetryRows.length || 0} samples`
    : `${diagnostics.row_count || telemetryRows.length || 0} samples   |   source ${sourceStart}–${sourceEnd}s`;
  document.querySelector("#telemetryRange").textContent =
    `${sourceLabel}   |   ignition T0 = ${zero}s${telemetryZeroAuto ? " (AUTO)" : " (MANUAL)"}`;
  const peakPressure = formatTelemetryNumber(diagnostics.peak_pressure, 2);
  const livePressure = runtimeTelemetry
    ? `${formatTelemetryNumber(runtimeTelemetry.pressure, 2)} ${form.elements.pressure_unit.value || "bar"}`
    : "—";
  const peakThrust = hasThrust
    ? `${formatTelemetryNumber(diagnostics.peak_thrust, 1)} ${form.elements.thrust_unit.value || "N"}`
    : "N/A — لا يوجد عمود دفع في الملف";
  document.querySelector("#telemetryChannels").textContent =
    `LIVE PRESSURE ${livePressure}   |   PEAK ${peakPressure} ${form.elements.pressure_unit.value || "bar"}   |   THRUST ${peakThrust}`;
}
function escapeHtml(value) {
  const div = document.createElement("div"); div.textContent = value; return div.innerHTML;
}

function renderScene() {
  if (!scene) return;
  elementHost.innerHTML = "";
  const elements = [...scene.elements].sort((a, b) => (a.z || 0) - (b.z || 0));
  for (const element of elements) {
    if (element.type === "video") continue;
    if (element.type === "chart") {
      positionNode(chartSvg, element);
      chartSvg.style.opacity = element.visible === false ? "0" : String(element.opacity ?? 1);
    }
    const node = document.createElement("div");
    node.className = `scene-element ${element.type} ${["pressure_card","thrust_card"].includes(element.type) ? "card" : ""}`;
    node.dataset.id = element.id;
    positionNode(node, element);
    node.style.zIndex = String((element.z || 0) + 40);
    node.style.opacity = element.visible === false ? ".12" : String(element.opacity ?? 1);
    node.style.background = element.type === "chart"
      ? "transparent"
      : hexAlpha(element.background || "#0F172A", element.backgroundOpacity || 0);
    node.style.color = element.color || "#fff";
    node.style.setProperty("--font-scale", String(element.fontSize || 1));
    if ((element.type === "identity" && !identityText()) ||
        (element.type === "logo" && !logoUrl)) node.classList.add("content-empty");
    if (element.id === selectedId) node.classList.add("selected");
    if (element.locked) node.classList.add("locked");
    node.innerHTML = `<div class="content">${elementContent(element)}</div><i class="resize-handle"></i>`;
    node.addEventListener("pointerdown", beginDrag);
    node.querySelector(".resize-handle").addEventListener("pointerdown", beginResize);
    elementHost.appendChild(node);
  }
  stage.style.background = scene.background || "#0B1017";
  renderLayers(); renderPreview();
}
function positionNode(node, element) {
  node.style.left = `${element.x * 100}%`; node.style.top = `${element.y * 100}%`;
  node.style.width = `${element.width * 100}%`; node.style.height = `${element.height * 100}%`;
  if (element.type === "chart") node.style.borderRadius = `${element.borderRadius || 0}px`;
}
function elementContent(element) {
  if (element.type === "title") return `<span class="eyebrow">STATIC FIRE TEST</span><strong>${escapeHtml(form.elements.title.value)}</strong>`;
  if (element.type === "timecode") return `<span class="eyebrow">MISSION TIME</span><strong id="liveTimecode">T− 00:00.00</strong>`;
  if (element.type === "identity") return escapeHtml(identityText());
  if (element.type === "pressure_card") return `<b>CHAMBER PRESSURE</b><strong id="livePressure">0.00 ${escapeHtml(form.elements.pressure_unit.value)}</strong>`;
  if (element.type === "thrust_card") return `<b>THRUST</b><strong id="liveThrust">0.0 ${escapeHtml(form.elements.thrust_unit.value)}</strong>`;
  if (element.type === "status") return `<span class="live-dot"></span><span id="liveStatus">STANDBY</span>`;
  if (element.type === "logo") return logoUrl ? `<img src="${logoUrl}" alt="" style="width:100%;height:100%;object-fit:contain">` : "";
  return "";
}
function identityText() {
  return ["run_number","motor_type","propellant","test_date"].map(name => form.elements[name].value).filter(Boolean).join(" • ");
}
function hexAlpha(hex, alpha) {
  const value = String(hex || "#000000").replace("#", "");
  const r = parseInt(value.slice(0,2),16)||0, g=parseInt(value.slice(2,4),16)||0, b=parseInt(value.slice(4,6),16)||0;
  return `rgba(${r},${g},${b},${alpha})`;
}

function renderLayers() {
  const list = document.querySelector("#layersList"); list.innerHTML = "";
  [...scene.elements].sort((a,b)=>(b.z||0)-(a.z||0)).forEach(element => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `layer ${element.id === selectedId ? "active" : ""} ${element.visible === false ? "muted" : ""}`;
    button.innerHTML = `<span class="drag">⠿</span><span>${names[element.type]}</span><span class="lock">${element.locked ? "🔒" : element.visible === false ? "◌" : "◉"}</span>`;
    button.addEventListener("click", () => selectElement(element.id));
    list.appendChild(button);
  });
}
function selectElement(id) {
  selectedId = id;
  document.querySelectorAll(".scene-element").forEach(node => node.classList.toggle("selected", node.dataset.id === id));
  renderLayers();
  const element = selected();
  document.querySelector("#noSelection").classList.toggle("hidden", !!element);
  document.querySelector("#propertyEditor").classList.toggle("hidden", !element);
  document.querySelector("#selectedName").textContent = element ? names[element.type] : "اختر عنصرًا";
  if (!element) return;
  setPropertyValues(element);
}

function beginDrag(event) {
  const id = event.currentTarget.dataset.id;
  selectElement(id);
  const element = selected();
  if (element.locked || event.target.classList.contains("resize-handle")) return;
  snapshot();
  dragState = { mode:"move", startX:event.clientX, startY:event.clientY, x:element.x, y:element.y };
  event.currentTarget.setPointerCapture(event.pointerId);
}
function beginResize(event) {
  event.stopPropagation();
  const node = event.currentTarget.parentElement;
  selectElement(node.dataset.id);
  const element = selected();
  if (element.locked) return;
  snapshot();
  dragState = { mode:"resize", startX:event.clientX, startY:event.clientY, w:element.width, h:element.height };
  node.setPointerCapture(event.pointerId);
}
window.addEventListener("pointermove", event => {
  if (!dragState || !selected()) return;
  const rect = stage.getBoundingClientRect();
  const dx = (event.clientX - dragState.startX) / rect.width;
  const dy = (event.clientY - dragState.startY) / rect.height;
  const element = selected();
  if (dragState.mode === "move") {
    element.x = snap(clamp(dragState.x + dx, 0, 1 - element.width));
    element.y = snap(clamp(dragState.y + dy, 0, 1 - element.height));
  } else {
    element.width = snap(clamp(dragState.w + dx, .03, 1 - element.x));
    element.height = snap(clamp(dragState.h + dy, .03, 1 - element.y));
  }
  renderScene(); selectElement(element.id);
});
window.addEventListener("pointerup", () => { if (dragState) saveDraft(); dragState = null; });
function snap(value) {
  const points = [0,.05,.1,.25,.5,.75,.9,.95,1];
  const near = points.find(point => Math.abs(point - value) < .008);
  return near ?? Math.round(value * 1000) / 1000;
}

function bindProperties() {
  const geometry = { propX:"x", propY:"y", propW:"width", propH:"height" };
  Object.entries(geometry).forEach(([id,key]) => {
    const input = document.querySelector(`#${id}`);
    input.addEventListener("focus", beginPropertyEdit);
    input.addEventListener("input", () => {
      const element = selected(); if (!element) return;
      element[key] = Number(input.value) / 100;
      element[key] = clamp(element[key], key === "width" || key === "height" ? .01 : 0, 1);
      renderScene(); selectElement(element.id);
    });
    input.addEventListener("change", endPropertyEdit);
  });
  bindRange("propOpacity", "opacity", 100, "opacityValue");
  bindRange("propBackgroundOpacity", "backgroundOpacity", 100, "backgroundOpacityValue");
  bindRange("lineWidth", "lineWidth", 1, "lineWidthValue");
  bindRange("fillOpacity", "fillOpacity", 100, "fillOpacityValue");
  bindRange("borderRadius", "borderRadius", 1, null);
  bindRange("fontSize", "fontSize", 100, "fontSizeValue");
  bindColor("propColor", "color"); bindColor("propBackground", "background");
  bindColor("pressureColor", "pressureColor"); bindColor("thrustColor", "thrustColor");
  bindColor("gridColor", "gridColor"); bindColor("cursorColor", "cursorColor");
  ["showGrid","showAxes","showTitle"].forEach(key => {
    document.querySelector(`#${key}`).addEventListener("change", event => {
      snapshot(); selected()[key] = event.target.checked; renderScene(); selectElement(selectedId); saveDraft();
    });
  });
  document.querySelector("#chartType").addEventListener("change", event => {
    mutateSelected(element => element.chartType = event.target.value);
  });
  for (const key of ["startTime","endTime","fadeIn","fadeOut"]) {
    const input=document.querySelector(`#${key}`);
    input.addEventListener("focus",beginPropertyEdit);
    input.addEventListener("change",()=>{
      const element=selected(); if(!element)return;
      element[key]=input.value===""?null:Number(input.value);
      endPropertyEdit();
    });
  }
}
function beginPropertyEdit() { if (!propertyEditing) snapshot(); propertyEditing = true; }
function endPropertyEdit() { propertyEditing = false; saveDraft(); }
function bindRange(id, key, divisor, outputId) {
  const input = document.querySelector(`#${id}`);
  input.addEventListener("pointerdown", beginPropertyEdit);
  input.addEventListener("input", () => {
    const element = selected(); if (!element) return;
    element[key] = Number(input.value) / divisor;
    if (outputId) document.querySelector(`#${outputId}`).textContent =
      divisor === 100 ? `${input.value}%` : input.value;
    renderScene(); selectElement(element.id);
  });
  input.addEventListener("change", endPropertyEdit);
}
function bindColor(id, key) {
  document.querySelector(`#${id}`).addEventListener("input", event => {
    if (!propertyEditing) beginPropertyEdit();
    selected()[key] = event.target.value; renderScene(); selectElement(selectedId);
  });
  document.querySelector(`#${id}`).addEventListener("change", endPropertyEdit);
}
function setPropertyValues(element) {
  document.querySelector("#propX").value = (element.x * 100).toFixed(1);
  document.querySelector("#propY").value = (element.y * 100).toFixed(1);
  document.querySelector("#propW").value = (element.width * 100).toFixed(1);
  document.querySelector("#propH").value = (element.height * 100).toFixed(1);
  document.querySelector("#propOpacity").value = (element.opacity ?? 1) * 100;
  document.querySelector("#opacityValue").textContent = `${Math.round((element.opacity ?? 1)*100)}%`;
  document.querySelector("#propColor").value = element.color || "#ffffff";
  document.querySelector("#propBackground").value = element.background || "#0f172a";
  document.querySelector("#propBackgroundOpacity").value = (element.backgroundOpacity ?? 0) * 100;
  document.querySelector("#backgroundOpacityValue").textContent = `${Math.round((element.backgroundOpacity ?? 0)*100)}%`;
  document.querySelector("#toggleVisible").textContent = element.visible === false ? "إظهار" : "إخفاء";
  document.querySelector("#toggleLock").textContent = element.locked ? "فتح القفل" : "قفل";
  const chart = element.type === "chart";
  document.querySelector("#textProperties").classList.toggle("hidden", chart || element.type === "video" || element.type === "logo");
  document.querySelector("#fontSize").value = (element.fontSize ?? 1) * 100;
  document.querySelector("#fontSizeValue").textContent = `${Math.round((element.fontSize ?? 1)*100)}%`;
  for (const key of ["startTime","endTime","fadeIn","fadeOut"])
    document.querySelector(`#${key}`).value = element[key] ?? "";
  document.querySelector("#chartProperties").classList.toggle("hidden", !chart);
  if (chart) {
    document.querySelector("#chartType").value = element.chartType || "line";
    for (const key of ["pressureColor","thrustColor","gridColor","cursorColor"])
      document.querySelector(`#${key}`).value = element[key];
    document.querySelector("#lineWidth").value = element.lineWidth || 3;
    document.querySelector("#lineWidthValue").textContent = element.lineWidth || 3;
    document.querySelector("#fillOpacity").value = (element.fillOpacity ?? .2)*100;
    document.querySelector("#fillOpacityValue").textContent = `${Math.round((element.fillOpacity ?? .2)*100)}%`;
    document.querySelector("#borderRadius").value = element.borderRadius || 0;
    for (const key of ["showGrid","showAxes","showTitle"]) document.querySelector(`#${key}`).checked = element[key] !== false;
  }
}

function renderPreview() {
  if (!scene) return;
  const chart = scene.elements.find(item => item.type === "chart");
  if (!chart) return;
  positionNode(chartSvg, chart);
  const time = video.currentTime || 0;
  const ignition = Number(form.elements.ignition_video_s.value) || 0;
  const telemetryTime = time - ignition;
  const series = normalizedSeries();
  const current = interpolate(series, telemetryTime);
  drawChart(series, telemetryTime, chart);
  const timecode = document.querySelector("#liveTimecode");
  if (timecode) timecode.textContent = `${telemetryTime >= 0 ? "T+" : "T−"} ${formatTime(Math.abs(telemetryTime))}`;
  const pressure = document.querySelector("#livePressure");
  if (pressure) pressure.textContent = `${current.pressure.toFixed(2)} ${form.elements.pressure_unit.value}`;
  const thrust = document.querySelector("#liveThrust");
  const hasThrust = Boolean(form.elements.thrust_column.value) && form.elements.thrust_column.value !== "__none__";
  if (thrust) thrust.textContent = hasThrust
    ? `${current.thrust.toFixed(1)} ${form.elements.thrust_unit.value}`
    : "N/A";
  const status = document.querySelector("#liveStatus");
  if (status) {
    const phase = telemetryTime < -.12 ? "STANDBY" : telemetryTime < .18 ? "IGNITION" : "TEST ACTIVE";
    status.textContent = phase;
    const tone = phase === "STANDBY" ? "#B8C4CC" : phase === "IGNITION" ? "#FFCB66" : "#B9F3CC";
    status.parentElement.style.color = tone;
    const dot = status.parentElement.querySelector(".live-dot");
    if (dot) dot.style.background = tone;
  }
  document.querySelector("#currentTime").textContent = formatTime(time);
  renderBroadcastPreview(series, telemetryTime, current);
}

function setBroadcastText(binding, value) {
  broadcastOverlay.querySelectorAll(`[data-bind="${binding}"]`)
    .forEach(node => { node.textContent = value; });
}

function invalidateBroadcastRasterWork() {
  broadcastRasterGeneration += 1;
  broadcastRasterPendingTime = null;
  if (broadcastRasterTimer) window.clearTimeout(broadcastRasterTimer);
  broadcastRasterTimer = null;
  if (broadcastRasterRequest) broadcastRasterRequest.abort();
  broadcastRasterRequest = null;
  broadcastRasterDisplayedSequence = broadcastRasterSequence;
}

function clearBroadcastRasterPreview() {
  invalidateBroadcastRasterWork();
  broadcastOverlay?.classList.remove("raster-ready", "raster-pending");
  if (broadcastRasterOverlay) broadcastRasterOverlay.removeAttribute("src");
  if (broadcastRasterUrl) URL.revokeObjectURL(broadcastRasterUrl);
  broadcastRasterUrl = null;
}

async function syncBroadcastPreviewLogo(renderAfter = true) {
  const previewId = broadcastPreviewSessionId;
  if (!previewId) return;
  try {
    let response;
    if (logoInput.files[0]) {
      const body = new FormData();
      body.append("logo", logoInput.files[0]);
      response = await fetch(`/api/previews/${previewId}/logo`, {
        method: "POST", body,
      });
    } else {
      response = await fetch(`/api/previews/${previewId}/logo`, {
        method: "DELETE",
      });
    }
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "تعذر تجهيز الشعار للمعاينة.");
    if (previewId !== broadcastPreviewSessionId) return;
    if (renderAfter) {
      invalidateBroadcastRasterWork();
      scheduleBroadcastRasterPreview();
    }
  } catch (error) {
    if (renderAfter) notify(error.message);
  }
}

function broadcastPreviewDimensions(realtime = !video.paused) {
  const sourceWidth = Math.max(320, Number(video.videoWidth) || 1920);
  const sourceHeight = Math.max(180, Number(video.videoHeight) || 1080);
  // Playback favors latency; pausing/scrubbing automatically refreshes the
  // same exact compositor at the larger inspection resolution.
  const maxWidth = realtime ? 960 : 1280;
  const maxHeight = realtime ? 540 : 720;
  const scale = Math.min(1, maxWidth / sourceWidth, maxHeight / sourceHeight);
  let width = Math.max(320, Math.round(sourceWidth * scale));
  let height = Math.max(180, Math.round(sourceHeight * scale));
  width -= width % 2;
  height -= height % 2;
  return { width, height };
}

function scheduleBroadcastRasterPreview(telemetryTimeArg) {
  if (!broadcastPreviewSessionId || !broadcastRasterOverlay) return;
  // While the user is dragging the playhead, only the lightweight DOM values
  // update. The exact current raster is requested once `seeked` fires.
  if (video.seeking) return;
  if (!broadcastOverlay.classList.contains("raster-ready")) {
    broadcastOverlay.classList.add("raster-pending");
  }
  const ignition = Number(form.elements.ignition_video_s.value) || 0;
  broadcastRasterPendingTime = telemetryTimeArg ?? ((video.currentTime || 0) - ignition);
  // Keep one request in flight and one latest pending time. Repeated video
  // frames only replace the pending value; they never cancel useful work.
  if (broadcastRasterRequest || broadcastRasterTimer) return;
  const minimumInterval = video.paused ? 0 : 80;
  const elapsed = performance.now() - broadcastRasterLastStartedAt;
  const delay = video.paused ? 25 : Math.max(0, minimumInterval - elapsed);
  broadcastRasterTimer = window.setTimeout(
    pumpBroadcastRasterPreview, delay
  );
}

function pumpBroadcastRasterPreview() {
  broadcastRasterTimer = null;
  if (broadcastRasterRequest || broadcastRasterPendingTime === null) return;
  const telemetryTime = broadcastRasterPendingTime;
  broadcastRasterPendingTime = null;
  void loadBroadcastRasterPreview(telemetryTime);
}

async function loadBroadcastRasterPreview(telemetryTime) {
  const previewId = broadcastPreviewSessionId;
  if (!previewId || !broadcastRasterOverlay) return;
  if (broadcastRasterRequest) {
    broadcastRasterPendingTime = telemetryTime;
    return;
  }
  const controller = new AbortController();
  broadcastRasterRequest = controller;
  const sequence = ++broadcastRasterSequence;
  const generation = broadcastRasterGeneration;
  broadcastRasterLastStartedAt = performance.now();
  const dimensions = broadcastPreviewDimensions(!video.paused);
  const payload = {
    broadcast_theme: selectedBroadcastTheme(),
    time: telemetryTime,
    title: form.elements.title.value || "ROCKET MOTOR STATIC TEST",
    subtitle: form.elements.subtitle.value || "STATIC FIRE TEST",
    time_column: form.elements.time_column.value,
    pressure_column: form.elements.pressure_column.value,
    thrust_column: form.elements.thrust_column.value || "__none__",
    pressure_unit: form.elements.pressure_unit.value || "bar",
    thrust_unit: form.elements.thrust_unit.value || "N",
    run_number: form.elements.run_number.value,
    motor_type: form.elements.motor_type.value,
    propellant: form.elements.propellant.value,
    oxidizer: form.elements.oxidizer.value,
    fuel: form.elements.fuel.value,
    ablative_material: form.elements.ablative_material.value,
    test_date: form.elements.test_date.value,
    organization_name: form.elements.organization_name.value,
    test_site: form.elements.test_site.value,
    coordinates_text: form.elements.coordinates_text.value,
    footer_tagline: form.elements.footer_tagline.value,
    camera_label: form.elements.camera_label.value,
    capture_fps: form.elements.capture_fps.value,
    pressure_limit: form.elements.pressure_limit.value,
    telemetry_zero_s: form.elements.telemetry_zero_s.value,
    time_scale: form.elements.time_scale.value || 1,
    accent: document.querySelector("#broadcastAccent")?.value || "#38BDF8",
    show_chart: document.querySelector("#showBroadcastChart")?.checked !== false,
    show_phases: document.querySelector("#showBroadcastPhases")?.checked !== false,
    ...selectedTemplatePayload(),
    ...dimensions,
  };
  try {
    const response = await fetch(`/api/previews/${previewId}/render`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!response.ok) {
      const problem = await response.json().catch(() => ({}));
      throw new Error(problem.error || "تعذر إنشاء المعاينة المطابقة.");
    }
    if (generation !== broadcastRasterGeneration || previewId !== broadcastPreviewSessionId) return;
    const renderedTelemetry = {
      time: Number(response.headers.get("X-Telemetry-Time")),
      pressure: Number(response.headers.get("X-Telemetry-Pressure")),
      hasThrust: response.headers.get("X-Telemetry-Has-Thrust") === "true",
      thrust: Number(response.headers.get("X-Telemetry-Thrust")),
    };
    const backendPeakPressure = Number(response.headers.get("X-Telemetry-Peak-Pressure"));
    const backendPeakThrust = Number(response.headers.get("X-Telemetry-Peak-Thrust"));
    const blob = await response.blob();
    if (generation !== broadcastRasterGeneration || previewId !== broadcastPreviewSessionId) return;
    const nextUrl = URL.createObjectURL(blob);
    const preloader = new Image();
    preloader.onload = () => {
      if (generation !== broadcastRasterGeneration ||
          previewId !== broadcastPreviewSessionId ||
          sequence <= broadcastRasterDisplayedSequence) {
        URL.revokeObjectURL(nextUrl);
        return;
      }
      broadcastRasterDisplayedSequence = sequence;
      runtimeTelemetry = renderedTelemetry;
      telemetryDiagnosticsData = {
        ...(telemetryDiagnosticsData || {}),
        peak_pressure: Number.isFinite(backendPeakPressure)
          ? backendPeakPressure : telemetryDiagnosticsData?.peak_pressure,
        peak_thrust: Number.isFinite(backendPeakThrust)
          ? backendPeakThrust : null,
        has_thrust: renderedTelemetry.hasThrust,
      };
      const oldUrl = broadcastRasterUrl;
      broadcastRasterUrl = nextUrl;
      broadcastRasterOverlay.src = nextUrl;
      broadcastRasterOverlay.dataset.telemetryTime = String(renderedTelemetry.time);
      broadcastRasterOverlay.dataset.sequence = String(sequence);
      broadcastOverlay.classList.remove("raster-pending");
      broadcastOverlay.classList.add("raster-ready");
      lastRasterError = "";
      renderTelemetryDiagnostics();
      if (oldUrl) URL.revokeObjectURL(oldUrl);
    };
    preloader.onerror = () => URL.revokeObjectURL(nextUrl);
    preloader.src = nextUrl;
  } catch (error) {
    if (error.name !== "AbortError") {
      broadcastOverlay.classList.remove("raster-ready", "raster-pending");
      renderTelemetryDiagnostics(error.message);
      if (error.message !== lastRasterError) {
        lastRasterError = error.message;
        notify(error.message);
      }
    }
  } finally {
    const ownsActiveRequest = broadcastRasterRequest === controller;
    if (ownsActiveRequest) broadcastRasterRequest = null;
    if (ownsActiveRequest &&
        generation === broadcastRasterGeneration &&
        previewId === broadcastPreviewSessionId &&
        broadcastRasterPendingTime !== null) {
      scheduleBroadcastRasterPreview(broadcastRasterPendingTime);
    }
  }
}

function drawBroadcastThemeChart(svg, series, telemetryTime, current, peakPressure, maxTime, accent) {
  const width = Number(svg.dataset.chartWidth) || 430;
  const height = Number(svg.dataset.chartHeight) || 128;
  const theme = svg.closest("[data-theme-layout]")?.dataset.themeLayout || "launch";
  const full = series.filter(item => item.time >= 0 && item.time <= maxTime);
  if (full.length < 2) { svg.innerHTML = ""; return; }
  const x = item => clamp(item.time / maxTime, 0, 1) * width;
  const y = item => height - 2 - clamp(item.pressure / (peakPressure * 1.05 || 1), 0, 1) * (height - 14);
  const path = items => items.map((item, index) => `${index ? "L" : "M"}${x(item)},${y(item)}`).join(" ");
  const fullPath = path(full);
  const livePath = path(full.filter(item => item.time <= telemetryTime));
  const cursorX = clamp(telemetryTime / maxTime, 0, 1) * width;
  const cursorY = height - 2 - clamp(current.pressure / (peakPressure * 1.05 || 1), 0, 1) * (height - 14);
  const area = `<line x1="0" y1="${height / 2}" x2="${width}" y2="${height / 2}" stroke="rgba(255,255,255,.14)"/>`;
  svg.innerHTML = `${area}<line x1="0" y1="${height - 2}" x2="${width}" y2="${height - 2}" stroke="rgba(255,255,255,.34)"/><path d="${fullPath}" fill="none" stroke="rgba(255,255,255,.24)" stroke-width="2"/><path d="${livePath}" fill="none" stroke="${accent}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/><line x1="${cursorX}" y1="0" x2="${cursorX}" y2="${height}" stroke="rgba(255,255,255,.7)"/><circle cx="${cursorX}" cy="${cursorY}" r="4" fill="${accent}" stroke="#fff"/>`;
}

function renderBroadcastPreview(seriesArg, telemetryTimeArg, currentArg) {
  if (!broadcastOverlay) return;
  const series = seriesArg || normalizedSeries();
  const videoTime = video.currentTime || 0;
  const ignition = Number(form.elements.ignition_video_s.value) || 0;
  const telemetryTime = telemetryTimeArg ?? (videoTime - ignition);
  const current = currentArg || interpolate(series, telemetryTime);
  const pressureUnit = form.elements.pressure_unit.value || "bar";
  const thrustUnit = form.elements.thrust_unit.value || "N";
  const hasThrust = Boolean(form.elements.thrust_column.value) && form.elements.thrust_column.value !== "__none__";
  const accent = document.querySelector("#broadcastAccent")?.value || "#38bdf8";
  const peakPressure = Math.max(0, ...series.map(item => item.pressure || 0));
  const peakThrust = Math.max(0, ...series.map(item => item.thrust || 0));
  const threshold = peakPressure * .05;
  const activeSamples = series.filter(item => threshold > 0 && item.pressure >= threshold);
  const burnout = activeSamples.at(-1)?.time ?? series.at(-1)?.time ?? 1;
  const ignitionEnd = Math.min(.6, burnout * .1);
  const maxTime = Math.max(burnout + .5, .5);
  const pressureLevel = clamp(current.pressure / (peakPressure * 1.05 || 1), 0, 1);
  const thrustLevel = clamp(current.thrust / (peakThrust * 1.05 || 1), 0, 1);

  let statusText, statusColor;
  if (telemetryTime < 0) { statusText = "PRE-IGNITION"; statusColor = "#94a3b8"; }
  else if (telemetryTime <= burnout + .1) {
    statusText = selectedBroadcastTheme() === "stellar_console" ? "HOT FIRE" : "TEST ACTIVE";
    statusColor = "#4ade80";
  }
  else { statusText = "TEST COMPLETE"; statusColor = "#facc15"; }
  let phaseIndex;
  if (telemetryTime < 0) phaseIndex = 0;
  else if (telemetryTime < ignitionEnd) phaseIndex = 1;
  else if (telemetryTime < burnout) phaseIndex = 2;
  else phaseIndex = 3;
  let phaseProgress;
  if (telemetryTime < 0) phaseProgress = clamp((telemetryTime + 4) / 4, 0, 1) / 3;
  else if (telemetryTime < ignitionEnd) phaseProgress = (1 + clamp(telemetryTime / (ignitionEnd || 1), 0, 1)) / 3;
  else if (telemetryTime < burnout) phaseProgress = (2 + clamp((telemetryTime - ignitionEnd) / (burnout - ignitionEnd || 1), 0, 1)) / 3;
  else phaseProgress = 1;

  broadcastOverlay.style.setProperty("--broadcast-accent", accent);
  setBroadcastText("title", form.elements.title.value || "ROCKET MOTOR STATIC TEST");
  setBroadcastText("subtitle", (form.elements.subtitle.value || "STATIC FIRE TEST").toUpperCase());
  setBroadcastText("clock", `${telemetryTime < 0 ? "T-" : "T+"} ${formatTime(Math.abs(telemetryTime))}`);
  setBroadcastText("status", statusText);
  setBroadcastText("pressure", current.pressure.toFixed(1));
  setBroadcastText("pressure-unit", pressureUnit);
  setBroadcastText("thrust", hasThrust ? current.thrust.toFixed(0) : "N/A");
  setBroadcastText("thrust-unit", hasThrust ? thrustUnit : "");
  setBroadcastText("peak-pressure", `${peakPressure.toFixed(2)} ${pressureUnit}`);
  setBroadcastText("peak-thrust", hasThrust ? `${peakThrust.toFixed(1)} ${thrustUnit}` : "N/A");
  setBroadcastText("peak-pressure-value", peakPressure.toFixed(2));
  setBroadcastText("peak-thrust-value", hasThrust ? peakThrust.toFixed(1) : "N/A");
  setBroadcastText("burn-time", `T+ ${ignitionEnd.toFixed(1)}s`);
  setBroadcastText("burnout-time", `T+ ${burnout.toFixed(1)}s`);
  setBroadcastText("chart-duration", `T 0–${maxTime.toFixed(0)} s`);
  setBroadcastText("chart-duration-short", `${maxTime.toFixed(0)}s`);
  setBroadcastText("full-thrust-phase", hasThrust ? "FULL THRUST" : "PRESSURE RISE");
  setBroadcastText("organization-name", form.elements.organization_name.value || "STELLAR KINETICS");
  setBroadcastText("test-site", form.elements.test_site.value || "ETLAQ SPACEPORT - DUQM, OMAN");
  const testMetadata = [
    form.elements.run_number.value,
    form.elements.motor_type.value,
    form.elements.test_date.value,
  ].filter(Boolean).join(" | ");
  setBroadcastText("test-metadata", testMetadata);
  setBroadcastText("footer-tagline", form.elements.footer_tagline.value || "STELLAR KINETICS - ENGINEERING THE VOID");
  setBroadcastText("footer-right", [
    form.elements.propellant.value || "LOX / PROPANE",
    form.elements.camera_label.value || "CAM 01",
    form.elements.capture_fps.value || "120 FPS",
  ].filter(Boolean).join(" - "));
  [1, .75, .5, .25, 0].forEach((fraction, index) => {
    setBroadcastText(`pressure-tick-${index}`, (peakPressure * 1.05 * fraction).toFixed(0));
  });

  broadcastOverlay.querySelectorAll(".theme-logo").forEach(logo => {
    logo.src = logoUrl || "";
    logo.style.display = logoUrl ? "block" : "none";
  });
  broadcastOverlay.querySelectorAll(".stellar-brand-fallback").forEach(label => {
    label.style.display = logoUrl ? "none" : "block";
  });
  broadcastOverlay.querySelectorAll("[data-status-wrap]").forEach(node => {
    node.style.color = statusColor;
  });
  broadcastOverlay.querySelectorAll("[data-requires-thrust]").forEach(node => {
    node.classList.remove("hidden");
    node.classList.toggle("channel-unavailable", !hasThrust);
  });
  document.querySelector(".range-readouts")?.classList.remove("single");

  broadcastOverlay.querySelectorAll("[data-pressure-arc]").forEach(node => {
    node.style.strokeDasharray = `${75 * pressureLevel} 100`;
  });
  broadcastOverlay.querySelectorAll("[data-thrust-arc]").forEach(node => {
    node.style.strokeDasharray = `${75 * thrustLevel} 100`;
  });
  broadcastOverlay.querySelectorAll("[data-pressure-fill]").forEach(node => {
    node.style.setProperty("--meter-level", `${pressureLevel * 100}%`);
  });
  broadcastOverlay.querySelectorAll("[data-thrust-fill]").forEach(node => {
    node.style.setProperty("--meter-level", `${thrustLevel * 100}%`);
  });
  broadcastOverlay.querySelectorAll("[data-phase-progress]").forEach(node => {
    node.style.width = `${phaseProgress * 100}%`;
  });
  broadcastOverlay.querySelectorAll("[data-phase-index]").forEach(node => {
    const index = Number(node.dataset.phaseIndex);
    node.classList.toggle("reached", index <= phaseIndex);
    node.classList.toggle("active", index === phaseIndex);
  });
  const burnLabel = broadcastOverlay.querySelector("[data-burn-label]");
  const burnoutLabel = broadcastOverlay.querySelector("[data-burnout-label]");
  if (burnLabel) burnLabel.style.setProperty("--x", `${clamp(ignitionEnd / maxTime, 0, 1) * 100}%`);
  if (burnoutLabel) burnoutLabel.style.setProperty("--x", `${clamp(burnout / maxTime, 0, 1) * 100}%`);

  broadcastOverlay.querySelectorAll("[data-theme-chart]").forEach(svg => {
    drawBroadcastThemeChart(svg, series, telemetryTime, current, peakPressure, maxTime, accent);
  });

  const showChart = document.querySelector("#showBroadcastChart").checked;
  const showPhases = document.querySelector("#showBroadcastPhases").checked;
  broadcastOverlay.querySelector('[data-theme-layout="launch"] [data-chart-wrap]')?.classList.toggle("hidden", !showChart);
  broadcastOverlay.querySelector('[data-theme-layout="mission_control"] [data-chart-wrap]')?.classList.toggle("hidden", !showChart);
  broadcastOverlay.querySelectorAll("[data-phase-track]").forEach(node => node.classList.toggle("hidden", !showPhases));
  broadcastOverlay.querySelectorAll("[data-phase-list]")
    .forEach(node => node.classList.toggle("hidden", !showPhases));
  scheduleBroadcastRasterPreview(telemetryTime);
}
function normalizedSeries() {
  const tKey=form.elements.time_column.value,pKey=form.elements.pressure_column.value,fKey=form.elements.thrust_column.value;
  const scale=Number(form.elements.time_scale.value)||1;
  const raw = telemetryRows
    .map(row=>({time:Number(row[tKey]),pressure:Number(row[pKey]),thrust:!fKey||fKey==="__none__"?0:Number(row[fKey])}))
    .filter(row=>Number.isFinite(row.time)&&Number.isFinite(row.pressure))
    .sort((a,b)=>a.time-b.time);
  const zeroText=form.elements.telemetry_zero_s.value;
  const zero=zeroText!==""?Number(zeroText):(raw[0]?.time||0);
  return raw.map(row=>({...row,time:(row.time-zero)*scale}));
}
function interpolate(series,time) {
  if (!series.length) return {pressure:0,thrust:0};
  if (time <= series[0].time) return {pressure:series[0].pressure,thrust:series[0].thrust};
  if (time >= series.at(-1).time) return {pressure:series.at(-1).pressure,thrust:series.at(-1).thrust};
  let right=series.findIndex(item=>item.time>=time); if(right<=0)return series[0];
  const a=series[right-1],b=series[right],ratio=(time-a.time)/(b.time-a.time||1);
  return {pressure:a.pressure+(b.pressure-a.pressure)*ratio,thrust:a.thrust+(b.thrust-a.thrust)*ratio};
}
function drawChart(series,time,style) {
  chartSvg.setAttribute("viewBox","0 0 1000 600");
  if (!series.length) { chartSvg.innerHTML=""; return; }
  const visible=document.querySelector("#revealChart").checked?series.filter(item=>item.time<=time):series;
  const maxP=Math.max(1,...series.map(item=>item.pressure)),maxF=Math.max(1,...series.map(item=>item.thrust));
  if (style.hudMode) {
    chartSvg.setAttribute("viewBox","0 0 1800 180");
    const min=series[0].time,max=series.at(-1).time;
    const hx=t=>355+(t-min)/(max-min||1)*850,hy=p=>142-p/maxP*102;
    const path=visible.map((item,index)=>`${index?"L":"M"}${hx(item.time).toFixed(1)},${hy(item.pressure).toFixed(1)}`).join(" ");
    const cursor=clamp(hx(time),355,1205),live=interpolate(series,time);
    let grid="",ticks="";
    for(let i=0;i<=2;i++){const y=142-i*51;if(style.showGrid!==false)grid+=`<line x1="355" y1="${y}" x2="1205" y2="${y}" stroke="${style.gridColor}" opacity=".22"/>`;}
    for(let i=0;i<=4;i++){const value=min+(max-min)*i/4,x=hx(value);if(style.showGrid!==false)grid+=`<line x1="${x}" y1="35" x2="${x}" y2="142" stroke="${style.gridColor}" opacity=".12"/>`;if(style.showAxes!==false)ticks+=`<text x="${x}" y="164" text-anchor="middle" fill="#AEBEC9" font-size="12">${value.toFixed(1)}s</text>`;}
    const fill=path&&visible.length?`<path d="${path} L${hx(visible.at(-1).time)},142 L${hx(visible[0].time)},142 Z" fill="${style.pressureColor}" opacity="${style.fillOpacity||.1}"/>`:"";
    chartSvg.innerHTML=`<rect width="1800" height="180" rx="8" fill="${hexAlpha(style.background,style.backgroundOpacity||0)}"/><rect x="0" y="0" width="5" height="180" fill="${style.pressureColor}"/><text x="42" y="45" fill="#93A7B5" font-size="13" font-weight="700" letter-spacing="2">LIVE TELEMETRY</text><text x="42" y="82" fill="#FFFFFF" font-size="24" font-weight="700">${escapeHtml(form.elements.subtitle.value)}</text><text x="42" y="116" fill="#B8C7D1" font-size="14">PEAK  ${maxP.toFixed(2)} ${escapeHtml(form.elements.pressure_unit.value)}</text><line x1="320" y1="28" x2="320" y2="152" stroke="#78909F" opacity=".28"/>${grid}${ticks}${fill}<path d="${path}" fill="none" stroke="${style.pressureColor}" stroke-width="4" stroke-linejoin="round" stroke-linecap="round"/><line x1="${cursor}" y1="34" x2="${cursor}" y2="142" stroke="${style.cursorColor}" opacity=".55" stroke-width="1"/><circle cx="${cursor}" cy="${hy(live.pressure)}" r="5" fill="#050B11" stroke="${style.pressureColor}" stroke-width="3"/><line x1="1260" y1="28" x2="1260" y2="152" stroke="#78909F" opacity=".28"/>`;
    return;
  }
  const minT=series[0].time,maxT=series.at(-1).time,px=t=>115+(t-minT)/(maxT-minT||1)*805,py=p=>500-p/maxP*365,fy=f=>500-f/maxF*365;
  const pathFor=(items,yValue)=>{
    if(style.chartType==="step")return items.map((item,index)=>{
      const x=px(item.time).toFixed(1),y=yValue(item).toFixed(1);
      return index?`H${x} V${y}`:`M${x},${y}`;
    }).join(" ");
    return items.map((item,index)=>`${index?"L":"M"}${px(item.time).toFixed(1)},${yValue(item).toFixed(1)}`).join(" ");
  };
  const pPath=pathFor(visible,item=>py(item.pressure));
  const fPath=pathFor(visible,item=>fy(item.thrust));
  const cursor=clamp(px(time),115,920);
  const live=interpolate(series,time);
  let grid="";
  let ticks="";
  const pUnit=escapeHtml(form.elements.pressure_unit.value),fUnit=escapeHtml(form.elements.thrust_unit.value);
  for(let i=0;i<=5;i++){
    const y=500-i*73,pValue=maxP*i/5;
    if(style.showGrid!==false)grid+=`<line x1="115" y1="${y}" x2="920" y2="${y}" stroke="${style.gridColor}" opacity=".46"/>`;
    if(style.showAxes!==false)ticks+=`<text x="98" y="${y+8}" text-anchor="end" fill="#F0F5F8" font-size="23" font-weight="650">${pValue.toFixed(maxP<10?1:0)}</text>`;
  }
  for(let i=0;i<=4;i++){
    const value=minT+(maxT-minT)*i/4,x=px(value);
    if(style.showGrid!==false)grid+=`<line x1="${x}" y1="135" x2="${x}" y2="500" stroke="${style.gridColor}" opacity=".20"/>`;
    if(style.showAxes!==false)ticks+=`<text x="${x}" y="538" text-anchor="middle" fill="#E8EFF3" font-size="22" font-weight="650">${value.toFixed(1)}</text>`;
  }
  const title=style.showTitle!==false?`<text x="42" y="47" fill="#FFFFFF" font-size="32" font-weight="750">${escapeHtml(form.elements.subtitle.value)}</text><text x="42" y="81" fill="#CDDAE2" font-size="20" font-weight="650">PEAK  ${maxP.toFixed(2)} ${pUnit}</text><line x1="42" y1="101" x2="958" y2="101" stroke="${style.gridColor}" opacity=".72"/>`:"";
  const legend=`<circle cx="690" cy="48" r="7" fill="${style.pressureColor}"/><text x="706" y="55" fill="#DDE7EE" font-size="19" font-weight="600">PRESSURE</text>${maxF>1?`<circle cx="850" cy="48" r="7" fill="${style.thrustColor}"/><text x="866" y="55" fill="#DDE7EE" font-size="19" font-weight="600">THRUST</text>`:""}`;
  const axes=style.showAxes!==false?`<line x1="115" y1="135" x2="115" y2="500" stroke="#7890A2" stroke-width="2"/><line x1="115" y1="500" x2="920" y2="500" stroke="#7890A2" stroke-width="2"/><text x="518" y="580" text-anchor="middle" fill="#C8D5DE" font-size="20" font-weight="700">TIME FROM IGNITION  (s)</text><text x="28" y="318" text-anchor="middle" fill="${style.pressureColor}" font-size="20" font-weight="700" transform="rotate(-90 28 318)">PRESSURE  (${pUnit})</text>`:"";
  const fill=pPath&&visible.length&&style.chartType==="area"?`<path d="${pPath} L${px(visible.at(-1).time)},500 L${px(visible[0].time)},500 Z" fill="${style.pressureColor}" opacity="${style.fillOpacity||0}"/>`:"";
  const traces=style.chartType==="points"
    ? visible.map(item=>`<circle cx="${px(item.time)}" cy="${py(item.pressure)}" r="${(style.lineWidth||3)+1}" fill="${style.pressureColor}"/><circle cx="${px(item.time)}" cy="${fy(item.thrust)}" r="${(style.lineWidth||3)+1}" fill="${style.thrustColor}"/>`).join("")
    : `<path d="${pPath}" fill="none" stroke="${style.pressureColor}" stroke-width="${style.lineWidth||3}" stroke-linejoin="round" stroke-linecap="round"/><path d="${fPath}" fill="none" stroke="${style.thrustColor}" stroke-width="${style.lineWidth||3}" stroke-linejoin="round"/>`;
  const livePoint=visible.length?`<circle cx="${cursor}" cy="${py(live.pressure)}" r="10" fill="#07121C" stroke="#F8FAFC" stroke-width="3"/><circle cx="${cursor}" cy="${py(live.pressure)}" r="5" fill="${style.pressureColor}"/>`:"";
  chartSvg.innerHTML=`<rect width="1000" height="600" rx="${style.borderRadius||0}" fill="${hexAlpha(style.background,style.backgroundOpacity||0)}"/>${grid}${axes}${ticks}${title}${legend}${fill}${traces}<line x1="${cursor}" y1="135" x2="${cursor}" y2="500" stroke="${style.cursorColor}" opacity=".8" stroke-width="2" stroke-dasharray="8 7"/>${livePoint}`;
}

document.querySelector("#playButton").addEventListener("click",()=>video.paused?video.play():video.pause());
document.querySelector("#timelineRange").addEventListener("input",event=>{if(video.duration)video.currentTime=Number(event.target.value)/1000*video.duration});
document.querySelector("#jumpIgnition").addEventListener("click",()=>video.currentTime=clamp(Number(form.elements.ignition_video_s.value)||0,0,video.duration||0));
function updateIgnitionMarker(){const d=video.duration||1,t=Number(form.elements.ignition_video_s.value)||0;document.querySelector("#ignitionMarker").style.left=`${clamp(t/d*100,0,100)}%`}
function formatTime(seconds){if(!Number.isFinite(seconds))return"00:00.00";const m=Math.floor(seconds/60),s=seconds-m*60;return`${String(m).padStart(2,"0")}:${s.toFixed(2).padStart(5,"0")}`}

document.querySelector("#toggleVisible").addEventListener("click",()=>mutateSelected(el=>el.visible=el.visible===false));
document.querySelector("#toggleLock").addEventListener("click",()=>mutateSelected(el=>el.locked=!el.locked));
document.querySelector("#moveUp").addEventListener("click",()=>mutateSelected(el=>el.z=(el.z||0)+1));
document.querySelector("#moveDown").addEventListener("click",()=>mutateSelected(el=>el.z=(el.z||0)-1));
document.querySelector("#resetElement").addEventListener("click",()=> {
  const original=initialScene.elements.find(item=>item.id===selectedId); if(!original)return;
  snapshot(); const index=scene.elements.findIndex(item=>item.id===selectedId); scene.elements[index]=clone(original); renderScene();selectElement(selectedId);saveDraft();
});
function mutateSelected(callback){if(!selected())return;snapshot();callback(selected());renderScene();selectElement(selectedId);saveDraft()}
document.querySelector("#undoButton").addEventListener("click",()=>{if(!undoStack.length)return;redoStack.push(JSON.stringify(scene));restore(undoStack.pop());saveDraft()});
document.querySelector("#redoButton").addEventListener("click",()=>{if(!redoStack.length)return;undoStack.push(JSON.stringify(scene));restore(redoStack.pop());saveDraft()});
document.querySelector("#saveTemplate").addEventListener("click",()=>{saveDraft();notify("تم حفظ التصميم في هذا المتصفح.")});
document.querySelector("#exportTemplate").addEventListener("click",()=>{
  const blob=new Blob([JSON.stringify(scene,null,2)],{type:"application/json"});
  const url=URL.createObjectURL(blob),link=document.createElement("a");
  link.href=url;link.download=`rocket-overlay-template-${new Date().toISOString().slice(0,10)}.json`;
  document.body.appendChild(link);link.click();link.remove();URL.revokeObjectURL(url);
  notify("تم تصدير القالب بنجاح.");
});
document.querySelector("#importTemplate").addEventListener("change",async event=>{
  const file=event.target.files[0];if(!file)return;
  try{
    const imported=JSON.parse(await file.text());
    if(!imported||!Array.isArray(imported.elements)||!imported.elements.length)throw new Error("ملف القالب غير صالح.");
    snapshot();scene=imported;renderScene();selectElement(null);saveDraft();notify("تم استيراد القالب بنجاح.");
  }catch(error){notify(error.message||"تعذر استيراد القالب.");}
  event.target.value="";
});
document.querySelector("#resetScene").addEventListener("click",()=>{snapshot();scene=clone(initialScene);renderScene();selectElement(null);saveDraft()});
function saveDraft(){localStorage.setItem("rocket-overlay-scene",JSON.stringify(scene))}

const templates={
  engineering:{chart:{x:.02,y:.805,width:.96,height:.165,backgroundOpacity:.93,chartType:"area",hudMode:true},pressure:{x:.71,y:.825,width:.125,height:.125},thrust:{x:.845,y:.825,width:.115,height:.125}},
  overlay:{chart:{x:.03,y:.57,width:.46,height:.35,backgroundOpacity:.68,chartType:"area"},pressure:{x:.51,y:.80,width:.20,height:.12},thrust:{x:.72,y:.80,width:.20,height:.12}},
  side:{chart:{x:.69,y:.16,width:.28,height:.56,backgroundOpacity:.94,chartType:"line"},pressure:{x:.69,y:.75,width:.135,height:.13},thrust:{x:.835,y:.75,width:.135,height:.13}},
  clean:{chart:{x:.035,y:.67,width:.44,height:.25,backgroundOpacity:.34,chartType:"line"},pressure:{x:.55,y:.82,width:.18,height:.10},thrust:{x:.75,y:.82,width:.18,height:.10}}
};
document.querySelectorAll("[data-template]").forEach(button=>button.addEventListener("click",()=>{
  snapshot();const preset=templates[button.dataset.template];
  Object.entries(preset).forEach(([id,values])=>Object.assign(scene.elements.find(item=>item.id===id),values));
  renderScene();saveDraft();
}));
document.querySelector("#stageZoom").addEventListener("input",event=>{const value=Number(event.target.value);document.querySelector("#zoomValue").textContent=`${value}%`;stage.style.width=`${value}%`});
function configureResolutionOptions(sourceWidth, sourceHeight) {
  const resolution = document.querySelector("#resolution");
  const sourceOption = resolution.querySelector('option[value="source"]');
  const hint = document.querySelector("#resolutionHint");
  const hasSourceSize = sourceWidth > 0 && sourceHeight > 0;
  sourceOption.textContent = hasSourceSize
    ? `الدقة الأصلية — ${sourceWidth}×${sourceHeight} (موصى بها)`
    : "الدقة الأصلية — موصى بها";

  const unavailable = [];
  resolution.querySelectorAll("option[data-native-check]").forEach(option => {
    const [width, height] = option.value.split("x").map(Number);
    const baseLabel = option.dataset.label;
    const sameShape = hasSourceSize
      && Math.abs(width / height - sourceWidth / sourceHeight) <= .01;
    const wouldUpscale = sameShape
      && (width > sourceWidth || height > sourceHeight);
    option.disabled = wouldUpscale;
    option.textContent = wouldUpscale
      ? `${baseLabel} — تكبير لا يحسن الجودة`
      : baseLabel;
    option.title = wouldUpscale
      ? `غير متاح: دقة المصدر ${sourceWidth}×${sourceHeight} أصغر من هذا المقاس.`
      : "";
    if (wouldUpscale) unavailable.push(baseLabel);
  });

  if (!hasSourceSize) {
    hint.textContent = "اختر الفيديو أولًا لتحديد دقته الأصلية ومنع التكبير غير المفيد.";
  } else if (unavailable.length) {
    hint.textContent = `المصدر ${sourceWidth}×${sourceHeight}؛ عُطّل ${unavailable.join(" و")} لأنه تكبير رقمي لا يضيف تفاصيل.`;
  } else {
    hint.textContent = `سيحافظ خيار الدقة الأصلية على أبعاد المصدر ${sourceWidth}×${sourceHeight}.`;
  }
}

function syncSourceQualityMode() {
  const enabled = document.querySelector("#preserveSourceQuality").checked;
  const resolution = document.querySelector("#resolution");
  const compatibilityCodec = form.elements.delivery_codec;
  const processingIds = ["enhanceVideo", "denoiseVideo", "stabilizeVideo"];
  const transformNames = ["crop_zoom", "crop_x", "crop_y", "outro_duration_s"];

  if (enabled) {
    resolution.value = "source";
    if (video.videoWidth && video.videoHeight) {
      stage.style.aspectRatio = `${video.videoWidth}/${video.videoHeight}`;
    }
    processingIds.forEach(id => { document.querySelector(`#${id}`).checked = false; });
    form.elements.crop_zoom.value = "1";
    form.elements.crop_x.value = ".5";
    form.elements.crop_y.value = ".5";
    form.elements.outro_duration_s.value = "0";
    updateCameraPreview();
  }
  resolution.disabled = enabled;
  compatibilityCodec.disabled = enabled;
  processingIds.forEach(id => { document.querySelector(`#${id}`).disabled = enabled; });
  transformNames.forEach(name => { form.elements[name].disabled = enabled; });
}

document.querySelector("#preserveSourceQuality").addEventListener("change", () => {
  syncSourceQualityMode();
  notify(
    document.querySelector("#preserveSourceQuality").checked
      ? "تم تفعيل الأرشفة HDR/HLG 10-bit: للمصادر المتوافقة فقط، بدقة المصدر وملف كبير جدًا."
      : "الوضع الافتراضي: SDR Rec.709 عالي الجودة بدقة المصدر، مطابق لإضاءة المعاينة."
  );
});

document.querySelector("#resolution").addEventListener("change",event=>{
  if(event.target.value==="source"){
    if(video.videoWidth&&video.videoHeight)stage.style.aspectRatio=`${video.videoWidth}/${video.videoHeight}`;
    return;
  }
  const[w,h]=event.target.value.split("x").map(Number);stage.style.aspectRatio=`${w}/${h}`;
});

function hidePreviousResults() {
  document.querySelector("#resultActions").classList.add("hidden");
  const download = document.querySelector("#downloadLink");
  const thumbnail = document.querySelector("#thumbnailLink");
  download.removeAttribute("href");
  thumbnail.removeAttribute("href");
  thumbnail.classList.add("hidden");
}

async function startExport(event) {
  event?.preventDefault();
  if(!videoInput.files[0]||!dataInput.files[0]){notify("اختر الفيديو وملف القياسات أولًا.");return}
  if(!scene){notify("انتظر اكتمال تحميل المحرر ثم حاول مرة أخرى.");return}
  clearTimeout(pollTimer);
  pollTimer = null;
  hidePreviousResults();
  const body=new FormData(form);
  const preserveSource = document.querySelector("#preserveSourceQuality").checked;
  body.set("preserve_source_quality", preserveSource ? "true" : "false");
  if (preserveSource) {
    body.set("resolution", "source");
    body.set("crop_zoom", "1");
    body.set("crop_x", ".5");
    body.set("crop_y", ".5");
    body.set("outro_duration_s", "0");
  }
  body.delete("scene_config");
  body.set("broadcast_theme", selectedBroadcastTheme());
  if (selectedTemplatePackage) {
    body.set("template_id", selectedTemplatePackage.id);
    body.set("template_version", selectedTemplatePackage.version);
    if (selectedTemplatePackage.sha256) body.set("sha256", selectedTemplatePackage.sha256);
    else body.delete("sha256");
  } else {
    body.delete("template_id");
    body.delete("template_version");
    body.delete("sha256");
  }
  body.set("accent",document.querySelector("#broadcastAccent").value);
  body.set("show_chart",document.querySelector("#showBroadcastChart").checked?"true":"false");
  body.set("show_phases",document.querySelector("#showBroadcastPhases").checked?"true":"false");
  for(const [name,id] of [["keep_audio","keepAudio"],["reveal_chart","revealChart"],["enhance_video","enhanceVideo"],["denoise_video","denoiseVideo"],["stabilize_video","stabilizeVideo"],["thumbnail","thumbnail"]])
    body.set(name,document.querySelector(`#${id}`).checked?"true":"false");
  const button=document.querySelector("#renderButton");button.disabled=true;
  button.textContent="جارٍ رفع الملفات…";
  document.querySelector("#progressBox").classList.remove("hidden");updateProgress(0,"جارٍ رفع الملفات — 0%");
  try{
    await uploadFilesInChunks(body);
    const payload=await uploadJob(body);
    const deliveryMode = preserveSource
      ? "HDR/HLG 10-bit الأرشيفي"
      : "SDR Rec.709 عالي الجودة";
    const startMessage = payload.notice
      ? `${payload.notice} — جارٍ بدء تصدير ${deliveryMode}`
      : `اكتمل الرفع — جارٍ بدء تصدير ${deliveryMode}`;
    if (payload.notice) notify(payload.notice);
    updateProgress(0,startMessage);
    button.textContent="جارٍ معالجة الفيديو…";
    pollJob(payload.id);
  }
  catch(error){hidePreviousResults();resetRenderButton();updateProgress(0,`خطأ: ${error.message}`);notify(error.message)}
}
form.addEventListener("submit", startExport);
document.querySelector("#renderButton").addEventListener("click", startExport);
async function uploadFilesInChunks(body) {
  const fields = [
    ["video",videoInput],["video_2",videoInput2],["video_3",videoInput3],
    ["data",dataInput],["logo",logoInput]
  ].filter(([,input])=>input.files[0]);
  const total = fields.reduce((sum,[,input])=>sum+input.files[0].size,0);
  let uploaded = 0;
  const chunkSize = 2 * 1024 * 1024;
  for (const [field,input] of fields) {
    const file = input.files[0];
    const initResponse = await fetch("/api/uploads/init", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({field,filename:file.name,size:file.size})
    });
    const initPayload = await initResponse.json();
    if (!initResponse.ok) throw new Error(initPayload.error||"تعذر بدء رفع الملف.");
    if (initPayload.reused) {
      uploaded += file.size;
      updateProgress(Math.round(uploaded/total*100),`تم استعادة ${file.name} — دون إعادة رفع`);
    }
    for (let offset=0; !initPayload.reused && offset<file.size; offset+=chunkSize) {
      const chunk=file.slice(offset,Math.min(file.size,offset+chunkSize));
      const response=await fetch(`/api/uploads/${initPayload.upload_id}`, {
        method:"POST", body:chunk,
        headers:{"Content-Type":"application/octet-stream"}
      });
      const payload=await response.json();
      if (!response.ok) throw new Error(payload.error||`تعذر رفع ${file.name}`);
      uploaded += chunk.size;
      const percent=Math.round(uploaded/total*100);
      updateProgress(percent,`جارٍ رفع الملفات — ${formatBytes(uploaded)} من ${formatBytes(total)}`);
    }
    body.delete(field);
    body.set(`${field}_token`,initPayload.upload_id);
  }
}
function uploadJob(body){
  return new Promise((resolve,reject)=>{
    const request=new XMLHttpRequest();
    request.open("POST","/api/jobs");
    request.addEventListener("load",()=>{
      let payload={};
      try{payload=JSON.parse(request.responseText||"{}")}catch{}
      if(request.status>=200&&request.status<300)resolve(payload);
      else reject(new Error(payload.error||`تعذر رفع الملفات (${request.status})`));
    });
    request.addEventListener("error",()=>reject(new Error("انقطع الاتصال أثناء رفع الملفات.")));
    request.addEventListener("abort",()=>reject(new Error("تم إلغاء رفع الملفات.")));
    request.send(body);
  });
}
function formatBytes(bytes){
  if(bytes<1024*1024)return`${(bytes/1024).toFixed(1)} KB`;
  return`${(bytes/1024/1024).toFixed(1)} MB`;
}
function updateProgress(value,message){document.querySelector("#progressValue").textContent=`${value}%`;document.querySelector("#progressBar").style.width=`${value}%`;document.querySelector("#progressMessage").textContent=message;document.querySelector("#systemStatus").textContent=value===100?"اكتمل":"جارٍ المعالجة"}
function resetRenderButton(){const button=document.querySelector("#renderButton");button.disabled=false;button.textContent="▶ تصدير الفيديو النهائي"}
function pollJob(id){clearTimeout(pollTimer);fetch(`/api/jobs/${id}`).then(r=>r.json()).then(job=>{updateProgress(job.progress||0,job.message);if(job.status==="complete"){document.querySelector("#downloadLink").href=job.result_url;const thumb=document.querySelector("#thumbnailLink");if(job.thumbnail_url){thumb.href=job.thumbnail_url;thumb.classList.remove("hidden")}document.querySelector("#resultActions").classList.remove("hidden");resetRenderButton();return}if(job.status==="error"){hidePreviousResults();resetRenderButton();updateProgress(job.progress||0,`خطأ: ${job.message}`);notify(job.message);return}pollTimer=setTimeout(()=>pollJob(id),1000)}).catch(error=>{hidePreviousResults();resetRenderButton();updateProgress(0,`خطأ: ${error.message}`);notify(error.message)})}

const storedBroadcastTheme = localStorage.getItem("rocket-overlay-broadcast-theme");
const savedBroadcastTheme = storedBroadcastTheme === "range_line"
  ? "stellar_console"
  : storedBroadcastTheme;
applyBroadcastTheme(
  savedBroadcastTheme && broadcastThemes[savedBroadcastTheme]
    ? savedBroadcastTheme
    : selectedBroadcastTheme()
);
void loadTemplatePackages();

initialize().then(() => {
  // Browsers may retain selected local files across a hard refresh without
  // firing change events. Rehydrate the broadcast preview in that case.
  if (videoInput.files[0]) videoInput.dispatchEvent(new Event("change"));
  if (videoInput2.files[0]) videoInput2.dispatchEvent(new Event("change"));
  if (videoInput3.files[0]) videoInput3.dispatchEvent(new Event("change"));
  if (logoInput.files[0]) logoInput.dispatchEvent(new Event("change"));
  if (dataInput.files[0]) dataInput.dispatchEvent(new Event("change"));
  syncSourceQualityMode();
  renderBroadcastPreview();
});
