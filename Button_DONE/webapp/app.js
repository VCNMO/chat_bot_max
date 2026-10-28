/* Портфолио педагога — логика мини-приложения MAX (v2) */
(function () {
  "use strict";

  var bridge = window.WebApp || null;
  var MB = 1024 * 1024;
  var MAX_CAPTURE_SIDE = 1800;
  var CAPTURE_QUALITY = 0.88;

  var screens = new Map();
  Array.prototype.forEach.call(document.querySelectorAll(".screen"), function (section) {
    screens.set(section.dataset.screen, section);
  });

  var state = {
    ready: false,
    screen: "loading",
    rootScreen: "loading",
    bootstrap: null,
    fields: {},
    summary: null,
    user: { fio: "" },
    profile: {},
    draft: null,
    formValues: {},
    submitting: false,
    fioSaving: false,
    toastTimer: null,
    camera: {
      stream: null,
      facing: "environment",
      mode: "idle",
      requestId: 0,
      blocked: false,
      resume: false
    }
  };

  // ---------------------------------------------------------------------
  // Утилиты
  // ---------------------------------------------------------------------

  function $(selector) {
    return document.querySelector(selector);
  }

  function $all(selector) {
    return Array.prototype.slice.call(document.querySelectorAll(selector));
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function icon(name, className) {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", className || "icon");
    svg.setAttribute("aria-hidden", "true");
    var use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#" + name);
    svg.appendChild(use);
    return svg;
  }

  function initData() {
    return (window.WebApp && window.WebApp.initData) || "";
  }

  async function api(path, options) {
    options = options || {};
    var headers = new Headers(options.headers || {});
    var raw = initData();
    if (raw) headers.set("X-Max-Init-Data", raw);

    var body = options.body;
    if (body && !(body instanceof FormData) && typeof body === "string") {
      headers.set("Content-Type", "application/json");
    }

    var response = await fetch(path, {
      method: options.method || "GET",
      headers: headers,
      body: body,
      credentials: "same-origin",
      cache: "no-store"
    });

    var data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }

    if (!response.ok) {
      var failure = new Error((data && data.error) || "Ошибка " + response.status);
      failure.status = response.status;
      failure.data = data;
      throw failure;
    }
    return data;
  }

  function showToast(message, type) {
    var toast = $("#toast");
    if (!toast) return;
    toast.textContent = message;
    toast.classList.toggle("is-error", type === "error");
    toast.hidden = false;
    window.clearTimeout(state.toastTimer);
    window.requestAnimationFrame(function () {
      toast.classList.add("is-visible");
    });
    state.toastTimer = window.setTimeout(function () {
      toast.classList.remove("is-visible");
      window.setTimeout(function () {
        toast.hidden = true;
      }, 220);
    }, 3600);
  }

  function haptic(kind) {
    try {
      var feedback = bridge && bridge.HapticFeedback;
      if (!feedback) return;
      if (kind === "success" && feedback.notificationOccurred) {
        feedback.notificationOccurred("success");
      } else if (feedback.impactOccurred) {
        feedback.impactOccurred("light");
      }
    } catch (error) {
      /* Haptic feedback не критичен. */
    }
  }

  function setUnsavedGuard(enabled) {
    if (!bridge) return;
    try {
      if (enabled && typeof bridge.enableClosingConfirmation === "function") {
        bridge.enableClosingConfirmation();
      } else if (!enabled && typeof bridge.disableClosingConfirmation === "function") {
        bridge.disableClosingConfirmation();
      }
    } catch (error) {
      /* старые версии Bridge могут не поддерживать метод */
    }
  }

  // ---------------------------------------------------------------------
  // Навигация
  // ---------------------------------------------------------------------

  function updateChrome() {
    var depth = (history.state && history.state.depth) || 0;
    var canGoBack = depth > 0 && state.screen !== "loading";
    var fioBack = $("#fio-back");
    if (fioBack) fioBack.hidden = !canGoBack;

    if (bridge && bridge.BackButton) {
      try {
        if (canGoBack) bridge.BackButton.show();
        else bridge.BackButton.hide();
      } catch (error) {
        /* Bridge может быть недоступен в обычном браузере */
      }
    }
  }

  function renderScreen(name) {
    var target = screens.get(name);
    if (!target) return;

    var previous = state.screen;
    screens.forEach(function (section, key) {
      section.hidden = key !== name;
    });
    state.screen = name;

    if (previous === "camera" && name !== "camera") {
      stopCamera();
      state.camera.mode = "idle";
    }

    if ((previous === "camera" || previous === "form") && name === "home") {
      resetDraft();
      setUnsavedGuard(false);
    }

    if (name === "home") renderHome();
    else if (name === "fio") renderFio();
    else if (name === "camera") startCamera();
    else if (name === "form") renderForm();

    updateChrome();
    window.scrollTo(0, 0);
  }

  function navigate(name) {
    if (state.screen === name) return;
    var depth = ((history.state && history.state.depth) || 0) + 1;
    history.pushState({ screen: name, depth: depth }, "");
    renderScreen(name);
  }

  function setRoot(name) {
    history.replaceState({ screen: name, depth: 0 }, "");
    state.rootScreen = name;
    renderScreen(name);
  }

  function goBack() {
    var depth = (history.state && history.state.depth) || 0;
    if (depth > 0) {
      history.back();
    } else if (state.screen !== state.rootScreen) {
      setRoot(state.rootScreen);
    }
  }

  function returnHome() {
    var depth = (history.state && history.state.depth) || 0;
    if (depth > 0) {
      history.go(-depth);
    } else {
      state.rootScreen = "home";
      history.replaceState({ screen: "home", depth: 0 }, "");
      renderScreen("home");
    }
  }

  window.addEventListener("popstate", function (event) {
    var entry = event.state;
    if (entry && entry.screen) renderScreen(entry.screen);
    else renderScreen(state.rootScreen || "home");
  });

  // ---------------------------------------------------------------------
  // Главный экран
  // ---------------------------------------------------------------------

  function emptyState() {
    var box = el("div", "empty");
    var iconBox = el("span", "empty-icon");
    iconBox.appendChild(icon("i-spark"));
    box.appendChild(iconBox);
    box.appendChild(el("p", "empty-title", "Пока ничего не загружено"));
    box.appendChild(
      el(
        "p",
        "empty-text",
        "Добавьте первую грамоту или курс — они появятся здесь и в общей сводке."
      )
    );
    return box;
  }

  function documentCard(doc) {
    var card = el("article", "doc-card");

    var thumb = el("div", "doc-thumb-wrap");
    thumb.appendChild(icon(doc.kind === "course" ? "i-cap" : "i-award", "icon doc-thumb-icon"));
    if (doc.photo_url) {
      var image = document.createElement("img");
      image.className = "doc-thumb";
      image.src = doc.photo_url;
      image.alt = "";
      image.decoding = "async";
      image.referrerPolicy = "no-referrer";
      image.addEventListener("error", function () {
        image.hidden = true;
      });
      thumb.appendChild(image);
    }

    var body = el("div", "doc-body");
    body.appendChild(el("h3", "doc-title", doc.title || "Без названия"));

    var meta = el("div", "doc-meta");
    if (doc.organizer) meta.appendChild(el("span", "", doc.organizer));
    if (doc.dates) meta.appendChild(el("span", "", doc.dates));
    if (doc.year) meta.appendChild(el("span", "", doc.year + " г."));
    body.appendChild(meta);

    var tags = el("div", "doc-tags");
    if (doc.kind === "competition") {
      if (doc.level) tags.appendChild(el("span", "tag tag-level", doc.level));
      if (doc.result) tags.appendChild(el("span", "tag tag-result", doc.result));
    } else if (doc.hours) {
      tags.appendChild(el("span", "tag tag-hours", doc.hours + " ч"));
    }
    if (tags.childNodes.length) body.appendChild(tags);

    card.appendChild(thumb);
    card.appendChild(body);
    return card;
  }

  function renderHome() {
    var summary = state.summary || {
      competitions: 0,
      courses: 0,
      hours: 0,
      documents: [],
      period_days: 365
    };

    $("#home-fio").textContent = state.user.fio || "ФИО не указано";
    $("#stat-competitions").textContent = String(summary.competitions || 0);
    $("#stat-courses").textContent = String(summary.courses || 0);
    $("#stat-hours").textContent = String(summary.hours || 0);

    var months = Math.max(1, Math.round((summary.period_days || 365) / 30));
    $("#home-period").textContent = months === 12 ? "12 месяцев" : months + " мес.";

    var container = $("#documents");
    container.textContent = "";

    var docs = Array.isArray(summary.documents) ? summary.documents : [];
    if (!docs.length) {
      container.appendChild(emptyState());
      return;
    }

    var groups = [
      {
        title: "Грамоты и конкурсы",
        icon: "i-award",
        items: docs.filter(function (doc) {
          return doc.kind === "competition";
        })
      },
      {
        title: "Курсы повышения квалификации",
        icon: "i-cap",
        items: docs.filter(function (doc) {
          return doc.kind === "course";
        })
      }
    ];

    groups.forEach(function (group) {
      if (!group.items.length) return;
      var block = el("div", "doc-group");
      var heading = el("div", "doc-group-title");
      heading.appendChild(icon(group.icon));
      heading.appendChild(el("span", "", group.title));
      heading.appendChild(el("span", "", "· " + group.items.length));
      block.appendChild(heading);
      group.items.forEach(function (doc) {
        block.appendChild(documentCard(doc));
      });
      container.appendChild(block);
    });
  }

  // ---------------------------------------------------------------------
  // ФИО
  // ---------------------------------------------------------------------

  function renderFio() {
    var editing = Boolean(state.user.fio);
    $("#fio-title").textContent = editing ? "Ваше ФИО" : "Как вас зовут?";
    $("#fio-lead").textContent = editing
      ? "Обновите ФИО — оно будет указано в сводке портфолио."
      : "Введите ФИО полностью — оно будет указано в сводке портфолио.";
    $("#fio-submit").textContent = editing ? "Сохранить" : "Продолжить";

    var input = $("#fio-input");
    input.value = state.user.fio || "";
    setFioError("");

    var profileName = [state.profile.last_name, state.profile.first_name]
      .filter(Boolean)
      .join(" ");
    var hint = $("#fio-profile-hint");
    if (profileName && !editing) {
      hint.textContent = "Профиль MAX: " + profileName + ". Можно дополнить отчество.";
      hint.hidden = false;
    } else {
      hint.hidden = true;
    }
  }

  function setFioError(message) {
    var error = $("#fio-error");
    var field = $("#fio-field");
    error.textContent = message || "";
    error.hidden = !message;
    if (field) field.classList.toggle("has-error", Boolean(message));
  }

  async function saveFio(event) {
    event.preventDefault();
    if (state.fioSaving) return;

    var input = $("#fio-input");
    var fio = input.value.replace(/\s+/g, " ").trim();
    var words = fio.split(" ").filter(Boolean);
    if (fio.length < 5 || words.length < 2) {
      setFioError("Введите фамилию и имя, лучше — ФИО полностью");
      input.focus();
      return;
    }

    setFioError("");
    var button = $("#fio-submit");
    var label = button.textContent;
    state.fioSaving = true;
    button.disabled = true;
    button.textContent = "Сохраняем…";

    try {
      var data = await api("/api/profile", {
        method: "POST",
        body: JSON.stringify({ fio: fio })
      });
      state.user.fio = data.user.fio;
      showToast(
        state.rootScreen === "home" ? "ФИО обновлено" : "Спасибо! Теперь добавим документы"
      );
      haptic("success");
      returnHome();
    } catch (error) {
      var message =
        error.data && error.data.fields && error.data.fields.fio
          ? error.data.fields.fio
          : error.message;
      setFioError(message || "Не удалось сохранить ФИО");
    } finally {
      state.fioSaving = false;
      button.disabled = false;
      button.textContent = label;
    }
  }

  // ---------------------------------------------------------------------
  // Камера
  // ---------------------------------------------------------------------

  function setCameraMode(mode) {
    state.camera.mode = mode;
    $("#camera-loading").hidden = mode !== "loading";
    $("#camera-live-controls").hidden = mode !== "live";
    $("#camera-preview-controls").hidden = mode !== "preview";
    $("#camera-error").hidden = mode !== "error";
    $("#camera-guide").hidden = mode === "error" || mode === "preview" || mode === "loading";
    $("#camera-preview").hidden = mode !== "preview";
    $("#camera-video").hidden = mode !== "live" && mode !== "loading";
    $("#camera-stage").classList.toggle("is-preview", mode === "preview");
    var shutter = $("#capture-button");
    if (shutter) shutter.disabled = mode !== "live";
  }

  function showCameraLoading(message) {
    $("#camera-loading-text").textContent = message || "Включаем камеру…";
    setCameraMode("loading");
  }

  function cameraErrorMessage(error) {
    if (!window.isSecureContext) {
      return "Камера доступна только по HTTPS. Откройте приложение по защищённому адресу.";
    }
    var name = (error && error.name) || "";
    if (name === "NotAllowedError" || name === "SecurityError") {
      return "Нет доступа к камере. Разрешите камеру для MAX в настройках устройства или браузера и попробуйте снова.";
    }
    if (name === "NotFoundError" || name === "OverconstrainedError") {
      return "Камера не найдена. Снимите документ камерой телефона или выберите готовое фото.";
    }
    if (name === "NotReadableError" || name === "AbortError") {
      return "Камера занята другим приложением. Закройте его и попробуйте снова.";
    }
    if (name === "NotSupportedError") {
      return "Этот браузер не поддерживает работу с камерой. Снимите документ камерой телефона.";
    }
    return "Не удалось включить камеру. Снимите документ камерой телефона или выберите готовое фото.";
  }

  function showCameraError(error) {
    stopCamera();
    state.camera.blocked = true;
    $("#camera-error-title").textContent = "Камера недоступна";
    $("#camera-error-text").textContent = cameraErrorMessage(error);
    $("#camera-hint").textContent =
      "Можно снять документ камерой телефона или выбрать готовое фото.";
    setCameraMode("error");
  }

  function stopCamera() {
    state.camera.requestId += 1;
    var stream = state.camera.stream;
    state.camera.stream = null;
    if (stream) {
      stream.getTracks().forEach(function (track) {
        try {
          track.stop();
        } catch (error) {
          /* уже остановлен */
        }
      });
    }
    var video = $("#camera-video");
    if (video) {
      try {
        video.pause();
      } catch (error) {
        /* видео могло быть ещё не запущено */
      }
      video.srcObject = null;
    }
  }

  function startCamera() {
    if (!state.draft) {
      returnHome();
      return;
    }
    $("#camera-title").textContent =
      state.draft.kind === "course" ? "Снимок удостоверения" : "Снимок грамоты";
    if (state.draft.url) {
      showCameraPreview();
    } else {
      state.camera.blocked = false;
      startCameraStream();
    }
  }

  async function startCameraStream() {
    stopCamera();
    showCameraLoading("Включаем камеру…");

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showCameraError({ name: "NotSupportedError" });
      return;
    }

    var requestId = ++state.camera.requestId;
    var constraints = {
      audio: false,
      video: {
        facingMode: { ideal: state.camera.facing },
        width: { ideal: 1920 },
        height: { ideal: 1080 }
      }
    };

    try {
      var stream = await navigator.mediaDevices.getUserMedia(constraints);
      if (
        requestId !== state.camera.requestId ||
        state.screen !== "camera" ||
        (state.draft && state.draft.url)
      ) {
        stream.getTracks().forEach(function (track) {
          track.stop();
        });
        return;
      }

      state.camera.stream = stream;
      var video = $("#camera-video");
      video.srcObject = stream;
      video.muted = true;
      video.playsInline = true;
      try {
        await video.play();
      } catch (error) {
        /* muted autoplay обычно разрешён; кадр всё равно доступен */
      }

      state.camera.blocked = false;
      setCameraMode("live");
      $("#camera-hint").textContent =
        "Положите документ на ровную поверхность и заполните кадр целиком.";
      updateFlipButton();
    } catch (error) {
      if (requestId !== state.camera.requestId) return;
      if (state.camera.facing === "user") {
        state.camera.facing = "environment";
        startCameraStream();
        return;
      }
      showCameraError(error);
    }
  }

  async function updateFlipButton() {
    var button = $("#camera-flip");
    if (!button) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
      button.hidden = false;
      return;
    }
    try {
      var devices = await navigator.mediaDevices.enumerateDevices();
      var count = devices.filter(function (device) {
        return device.kind === "videoinput";
      }).length;
      button.hidden = count < 2;
    } catch (error) {
      button.hidden = false;
    }
  }

  function showCameraPreview() {
    stopCamera();
    if (!state.draft || !state.draft.url) return;
    $("#camera-preview").src = state.draft.url;
    setCameraMode("preview");
    $("#camera-hint").textContent =
      "Проверьте: документ целиком в кадре, текст читается.";
  }

  function acceptPhoto(blob) {
    if (!state.draft) state.draft = { kind: "competition", blob: null, url: "" };
    if (state.draft.url) URL.revokeObjectURL(state.draft.url);
    state.draft.blob = blob;
    state.draft.url = URL.createObjectURL(blob);
    setUnsavedGuard(true);
    showCameraPreview();
  }

  function retakePhoto() {
    if (!state.draft) {
      returnHome();
      return;
    }
    var kind = state.draft.kind;
    if (state.draft.url) URL.revokeObjectURL(state.draft.url);
    state.draft = { kind: kind, blob: null, url: "" };
    state.camera.blocked = false;
    startCameraStream();
  }

  function captureFrame(video, width, height) {
    var scale = Math.min(1, MAX_CAPTURE_SIDE / Math.max(width, height));
    var canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    var context = canvas.getContext("2d");
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    return new Promise(function (resolve, reject) {
      canvas.toBlob(
        function (blob) {
          if (blob) resolve(blob);
          else reject(new Error("canvas.toBlob вернул null"));
        },
        "image/jpeg",
        CAPTURE_QUALITY
      );
    });
  }

  async function capturePhoto() {
    if (state.camera.mode !== "live" || !state.camera.stream) return;
    var video = $("#camera-video");
    var width = video.videoWidth || 0;
    var height = video.videoHeight || 0;
    if (!width || !height) {
      showToast("Камера ещё не готова. Подождите секунду.", "error");
      return;
    }

    var shutter = $("#capture-button");
    shutter.disabled = true;
    try {
      var blob = await captureFrame(video, width, height);
      acceptPhoto(blob);
      haptic("light");
    } catch (error) {
      showToast("Не удалось сделать снимок. Попробуйте ещё раз.", "error");
    } finally {
      shutter.disabled = false;
    }
  }

  function loadImageElement(file) {
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(file);
      var image = new Image();
      image.onload = function () {
        URL.revokeObjectURL(url);
        resolve(image);
      };
      image.onerror = function () {
        URL.revokeObjectURL(url);
        reject(new Error("Не удалось прочитать изображение"));
      };
      image.src = url;
    });
  }

  async function compressImage(file) {
    var source;
    if (typeof createImageBitmap === "function") {
      try {
        source = await createImageBitmap(file, { imageOrientation: "from-image" });
      } catch (error) {
        source = await loadImageElement(file);
      }
    } else {
      source = await loadImageElement(file);
    }

    var width = source.width || source.naturalWidth || 0;
    var height = source.height || source.naturalHeight || 0;
    if (!width || !height) throw new Error("Пустое изображение");

    var scale = Math.min(1, MAX_CAPTURE_SIDE / Math.max(width, height));
    var canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    var context = canvas.getContext("2d");
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(source, 0, 0, canvas.width, canvas.height);
    if (typeof source.close === "function") source.close();

    return new Promise(function (resolve, reject) {
      canvas.toBlob(
        function (blob) {
          if (blob) resolve(blob);
          else reject(new Error("canvas.toBlob вернул null"));
        },
        "image/jpeg",
        CAPTURE_QUALITY
      );
    });
  }

  async function onFilePicked(event) {
    var input = event.currentTarget;
    var file = input.files && input.files[0];
    input.value = "";
    if (!file) return;

    if (!/^image\//.test(file.type || "")) {
      showToast("Выберите файл изображения", "error");
      return;
    }
    if (file.size > 50 * MB) {
      showToast("Файл больше 50 МБ. Выберите фото поменьше.", "error");
      return;
    }

    showCameraLoading("Обрабатываем снимок…");
    try {
      var blob = await compressImage(file);
      acceptPhoto(blob);
    } catch (error) {
      if (state.camera.stream) {
        setCameraMode("live");
        $("#camera-hint").textContent =
          "Положите документ на ровную поверхность и заполните кадр целиком.";
      } else {
        showCameraError({ name: "NotReadableError" });
      }
      showToast("Не удалось прочитать изображение. Попробуйте другое фото.", "error");
    }
  }

  function flipCamera() {
    state.camera.facing = state.camera.facing === "environment" ? "user" : "environment";
    state.camera.blocked = false;
    startCameraStream();
  }

  // ---------------------------------------------------------------------
  // Анкета
  // ---------------------------------------------------------------------

  function clearFieldError(key) {
    var wrapper = document.querySelector('[data-field="' + key + '"]');
    var error = document.getElementById("error-" + key);
    if (wrapper) wrapper.classList.remove("has-error");
    if (error) {
      error.textContent = "";
      error.hidden = true;
    }
  }

  function setFieldError(key, message) {
    var wrapper = document.querySelector('[data-field="' + key + '"]');
    var error = document.getElementById("error-" + key);
    if (wrapper) wrapper.classList.add("has-error");
    if (error) {
      error.textContent = message || "";
      error.hidden = !message;
    }
  }

  function clearAllFormErrors() {
    $all("#document-form .field.has-error").forEach(function (field) {
      field.classList.remove("has-error");
    });
    $all("#document-form .field-error").forEach(function (error) {
      error.textContent = "";
      error.hidden = true;
    });
    $("#form-error").hidden = true;
  }

  function showFormError(message) {
    var box = $("#form-error");
    box.textContent = message;
    box.hidden = false;
  }

  function buildField(question) {
    if (question.kind === "enum") {
      var group = document.createElement("fieldset");
      group.className = "field field-group";
      group.dataset.field = question.key;

      var legend = el("legend", "field-label", question.label);
      group.appendChild(legend);

      var chips = el("div", "chips");
      question.options.forEach(function (option, index) {
        var chip = el("label", "chip");
        var input = document.createElement("input");
        input.type = "radio";
        input.name = question.key;
        input.value = option;
        input.id = "opt-" + question.key + "-" + index;
        input.checked = state.formValues[question.key] === option;
        input.addEventListener("change", function () {
          state.formValues[question.key] = option;
          clearFieldError(question.key);
        });
        chip.appendChild(input);
        chip.appendChild(el("span", "", option));
        chips.appendChild(chip);
      });
      group.appendChild(chips);

      var enumError = el("span", "field-error");
      enumError.id = "error-" + question.key;
      enumError.hidden = true;
      group.appendChild(enumError);
      return group;
    }

    var field = el("label", "field");
    field.dataset.field = question.key;
    field.appendChild(el("span", "field-label", question.label));

    var control = document.createElement("input");
    control.className = "field-input";
    control.id = "input-" + question.key;
    control.name = question.key;
    control.type = "text";
    control.autocomplete = "off";
    control.value =
      state.formValues[question.key] !== undefined
        ? String(state.formValues[question.key])
        : question.kind === "year"
          ? String(new Date().getFullYear())
          : "";

    if (question.kind === "text") {
      control.maxLength = 500;
      control.placeholder = question.hint || "";
    } else if (question.kind === "year") {
      control.inputMode = "numeric";
      control.maxLength = 4;
      control.placeholder = "2025";
    } else {
      control.inputMode = "numeric";
      control.maxLength = 5;
      control.placeholder = "72";
    }

    if (question.kind === "year" && state.formValues[question.key] === undefined) {
      state.formValues[question.key] = control.value;
    }

    control.addEventListener("input", function () {
      state.formValues[question.key] = control.value;
      clearFieldError(question.key);
    });
    field.appendChild(control);

    if (question.hint) {
      field.appendChild(el("span", "field-hint", "Например: " + question.hint));
    }

    var error = el("span", "field-error");
    error.id = "error-" + question.key;
    error.hidden = true;
    field.appendChild(error);
    return field;
  }

  function renderForm() {
    if (!state.draft || !state.draft.blob) {
      showToast("Сначала сделайте фото документа", "error");
      returnHome();
      return;
    }

    var kind = state.draft.kind;
    var questions = state.fields[kind] || [];
    $("#form-screen-title").textContent =
      kind === "course" ? "Новый курс ПК" : "Новая грамота";
    $("#form-title").textContent =
      kind === "course" ? "Данные курса" : "Данные конкурса";
    $("#form-lead").textContent =
      kind === "course"
        ? "Заполните сведения о курсе — они сохранятся вместе с фото."
        : "Заполните сведения о конкурсе — они сохранятся вместе с фото.";
    $("#draft-thumb").src = state.draft.url;

    var container = $("#form-fields");
    container.textContent = "";
    if (!questions.length) {
      showFormError("Сервер не вернул список полей. Обновите приложение.");
      return;
    }
    questions.forEach(function (question) {
      container.appendChild(buildField(question));
    });

    clearAllFormErrors();
    setSubmitting(false);
    setUnsavedGuard(true);
  }

  function collectValues(kind) {
    var values = {};
    (state.fields[kind] || []).forEach(function (question) {
      var value = state.formValues[question.key];
      if (value === undefined || value === null) value = "";
      values[question.key] = typeof value === "string" ? value.replace(/\s+/g, " ").trim() : value;
    });
    return values;
  }

  function validateForm(kind, values) {
    var errors = {};
    (state.fields[kind] || []).forEach(function (question) {
      var value = values[question.key];
      if (question.kind === "enum") {
        if ((question.options || []).indexOf(value) === -1) {
          errors[question.key] = "Выберите вариант из списка";
        }
      } else if (question.kind === "year") {
        var year = String(value || "");
        if (!/^\d{4}$/.test(year) || Number(year) < 1900 || Number(year) > 2100) {
          errors[question.key] = "Укажите год из 4 цифр, например 2025";
        }
      } else if (question.kind === "hours") {
        var hours = String(value || "");
        if (!/^\d{1,5}$/.test(hours) || Number(hours) < 1 || Number(hours) > 10000) {
          errors[question.key] = "Укажите целое число часов от 1 до 10 000";
        }
      } else if (question.kind === "text") {
        if (!value) errors[question.key] = "Заполните поле";
        else if (String(value).length > 500) errors[question.key] = "Не больше 500 символов";
      }
    });
    return errors;
  }

  function showErrors(errors) {
    var firstKey = null;
    Object.keys(errors).forEach(function (key) {
      if (key === "photo") {
        showFormError(errors[key]);
        return;
      }
      if (!firstKey) firstKey = key;
      setFieldError(key, errors[key]);
    });
    if (firstKey) {
      if ($("#form-error").hidden) {
        showFormError("Проверьте выделенные поля.");
      }
      var first = document.querySelector('[data-field="' + firstKey + '"] input');
      if (first && typeof first.focus === "function") {
        first.focus({ preventScroll: false });
        if (typeof first.scrollIntoView === "function") {
          first.scrollIntoView({ block: "center", behavior: "smooth" });
        }
      }
    }
  }

  function setSubmitting(busy) {
    state.submitting = busy;
    var button = $("#form-submit");
    button.disabled = busy;
    button.textContent = busy ? "Сохраняем…" : "Сохранить документ";
    $("#document-form").setAttribute("aria-busy", busy ? "true" : "false");
  }

  async function submitDocument(event) {
    event.preventDefault();
    if (state.submitting) return;

    var draft = state.draft;
    if (!draft || !draft.blob) {
      showToast("Сначала сделайте фото документа", "error");
      return;
    }

    var kind = draft.kind;
    var values = collectValues(kind);
    clearAllFormErrors();

    var errors = validateForm(kind, values);
    if (Object.keys(errors).length) {
      showErrors(errors);
      return;
    }

    var formData = new FormData();
    formData.append("kind", kind);
    formData.append("photo", draft.blob, "document.jpg");
    formData.append("fields", JSON.stringify(values));

    setSubmitting(true);
    $("#form-error").hidden = true;

    try {
      var data = await api("/api/documents", { method: "POST", body: formData });
      state.summary = data.summary || state.summary;
      resetDraft();
      setUnsavedGuard(false);
      haptic("success");
      showToast(kind === "course" ? "Курс сохранён" : "Грамота сохранена");
      returnHome();
    } catch (error) {
      if (error.status === 401) {
        showFormError("Сессия истекла. Перезапустите приложение из чата с ботом.");
      } else {
        if (error.data && error.data.fields) showErrors(error.data.fields);
        showFormError(error.message || "Не удалось сохранить документ. Попробуйте ещё раз.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  function resetDraft() {
    if (state.draft && state.draft.url) URL.revokeObjectURL(state.draft.url);
    state.draft = null;
    state.formValues = {};
  }

  function startNewDocument(kind) {
    if (!state.ready || !state.fields[kind]) return;
    resetDraft();
    state.draft = { kind: kind, blob: null, url: "" };
    state.formValues = {};
    setUnsavedGuard(false);
    navigate("camera");
  }

  function retakeFromForm() {
    if (!state.draft) {
      returnHome();
      return;
    }
    var kind = state.draft.kind;
    if (state.draft.url) URL.revokeObjectURL(state.draft.url);
    state.draft = { kind: kind, blob: null, url: "" };
    state.camera.blocked = false;
    setUnsavedGuard(false);

    // Возвращаемся к уже открытой камере, а не добавляем ещё один экран:
    // иначе «Назад» после пересъёмки проходил бы через лишние шаги.
    var depth = (history.state && history.state.depth) || 0;
    if (depth > 1) {
      history.back();
    } else {
      history.replaceState({ screen: "camera", depth: Math.max(1, depth) }, "");
      renderScreen("camera");
    }
  }

  // ---------------------------------------------------------------------
  // Запуск
  // ---------------------------------------------------------------------

  async function boot() {
    state.ready = false;
    $("#loading-spinner").hidden = false;
    $("#loading-text").textContent = "Загружаем данные…";
    $("#retry-button").hidden = true;
    renderScreen("loading");

    try {
      var data = await api("/api/bootstrap");
      state.bootstrap = data;
      state.fields = data.fields || {};
      state.summary = data.summary;
      state.user = data.user || { fio: "" };
      state.profile = data.profile || {};
      state.ready = true;

      $("#dev-banner").hidden = !data.dev_mode;
      $("#dev-pill").hidden = !data.dev_mode;

      if (!state.user.fio) {
        state.rootScreen = "fio";
        history.replaceState({ screen: "fio", depth: 0 }, "");
        renderScreen("fio");
      } else {
        state.rootScreen = "home";
        history.replaceState({ screen: "home", depth: 0 }, "");
        renderScreen("home");
      }
    } catch (error) {
      state.ready = false;
      $("#loading-spinner").hidden = true;
      $("#loading-text").textContent =
        error.status === 401
          ? "Приложение работает внутри MAX. Откройте его кнопкой в чате с ботом."
          : error.message || "Не удалось загрузить данные. Проверьте соединение.";
      $("#retry-button").hidden = false;
      renderScreen("loading");
    }
  }

  function bindEvents() {
    $("#add-competition").addEventListener("click", function () {
      startNewDocument("competition");
    });
    $("#add-course").addEventListener("click", function () {
      startNewDocument("course");
    });
    $("#edit-fio").addEventListener("click", function () {
      navigate("fio");
    });

    $("#fio-form").addEventListener("submit", saveFio);
    $("#fio-back").addEventListener("click", goBack);

    $("#camera-back").addEventListener("click", goBack);
    $("#capture-button").addEventListener("click", capturePhoto);
    $("#retake-button").addEventListener("click", retakePhoto);
    $("#use-photo-button").addEventListener("click", function () {
      if (state.draft && state.draft.blob) navigate("form");
      else showToast("Сначала сделайте снимок", "error");
    });
    $("#camera-flip").addEventListener("click", flipCamera);
    $("#camera-retry-button").addEventListener("click", function () {
      state.camera.blocked = false;
      startCameraStream();
    });
    $("#camera-gallery").addEventListener("click", function () {
      $("#gallery-input").click();
    });
    $("#camera-error-gallery").addEventListener("click", function () {
      $("#gallery-input").click();
    });
    $("#camera-file-button").addEventListener("click", function () {
      $("#camera-file-input").click();
    });
    $("#gallery-input").addEventListener("change", onFilePicked);
    $("#camera-file-input").addEventListener("change", onFilePicked);

    $("#form-back").addEventListener("click", goBack);
    $("#form-retake").addEventListener("click", retakeFromForm);
    $("#document-form").addEventListener("submit", submitDocument);

    $("#retry-button").addEventListener("click", boot);

    document.addEventListener("visibilitychange", function () {
      if (document.hidden) {
        if (state.screen === "camera" && state.camera.mode === "live") {
          state.camera.resume = true;
          stopCamera();
          showCameraLoading("Включаем камеру…");
        }
      } else if (state.screen === "camera" && state.camera.resume) {
        state.camera.resume = false;
        startCameraStream();
      }
    });
  }

  function init() {
    bindEvents();

    if (bridge && bridge.BackButton && typeof bridge.BackButton.onClick === "function") {
      try {
        bridge.BackButton.onClick(goBack);
      } catch (error) {
        /* Bridge может быть недоступен */
      }
    }

    history.replaceState({ screen: "loading", depth: 0 }, "");
    renderScreen("loading");
    boot();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
