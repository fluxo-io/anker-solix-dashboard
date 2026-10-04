(() => {
  "use strict";

  const SOURCE_SELECTOR = "[data-toast-message]";
  const SESSION_EXPIRED_KEY = "solarbank.sessionExpired";
  const TYPES = Object.freeze({
    success: { title: "Erfolg", symbol: "✓" },
    danger: { title: "Fehler", symbol: "!" },
    warning: { title: "Hinweis", symbol: "!" },
    info: { title: "Hinweis", symbol: "i" },
  });

  function normalizeType(type) {
    if (type === "error") {
      return "danger";
    }
    return Object.hasOwn(TYPES, type) ? type : "info";
  }

  function toastContainer() {
    const host = document.querySelector("dialog[open]") || document.body;
    let region = Array.from(host.children).find((child) =>
      child.matches?.("[data-toast-container]"),
    );
    if (region) {
      return region;
    }

    region = document.createElement("div");
    region.className = "app-toasts";
    region.dataset.toastContainer = "";
    region.setAttribute("role", "region");
    region.setAttribute("aria-label", "Meldungen");
    host.append(region);
    return region;
  }

  function removeToast(element) {
    if (element.classList.contains("is-leaving")) {
      return;
    }
    element.classList.add("is-leaving");
    element.classList.remove("is-visible");
    const remove = () => element.remove();
    element.addEventListener("transitionend", remove, { once: true });
    window.setTimeout(remove, 180);
  }

  function showToast(message, { type = "info", autohide, delay = 7000 } = {}) {
    const text = String(message ?? "").trim();
    if (!text) {
      return null;
    }

    const kind = normalizeType(type);
    const urgent = kind === "danger" || kind === "warning";
    const element = document.createElement("section");
    element.className = `app-toast app-toast-${kind}`;
    element.setAttribute("role", urgent ? "alert" : "status");
    element.setAttribute("aria-live", urgent ? "assertive" : "polite");
    element.setAttribute("aria-atomic", "true");

    const header = document.createElement("div");
    header.className = "toast-header";

    const icon = document.createElement("span");
    icon.className = "toast-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = TYPES[kind].symbol;

    const title = document.createElement("strong");
    title.className = "toast-title";
    title.textContent = TYPES[kind].title;

    const close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.setAttribute("aria-label", "Meldung schließen");
    close.textContent = "×";
    close.addEventListener("click", () => removeToast(element));
    header.append(icon, title, close);

    const body = document.createElement("div");
    body.className = "toast-body";
    body.textContent = text;
    element.append(header, body);
    toastContainer().append(element);
    window.requestAnimationFrame(() => element.classList.add("is-visible"));

    const shouldAutohide = autohide ?? !urgent;
    let timer = null;
    const clearTimer = () => {
      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
    };
    const scheduleRemoval = () => {
      clearTimer();
      if (shouldAutohide) {
        timer = window.setTimeout(() => removeToast(element), delay);
      }
    };
    element.addEventListener("mouseenter", clearTimer);
    element.addEventListener("mouseleave", scheduleRemoval);
    element.addEventListener("focusin", clearTimer);
    element.addEventListener("focusout", (event) => {
      if (!element.contains(event.relatedTarget)) {
        scheduleRemoval();
      }
    });
    scheduleRemoval();
    return element;
  }

  function sourceDetail(source) {
    return source.querySelector("[data-toast-text]") || source;
  }

  function processSource(source) {
    const detail = sourceDetail(source);
    const text = detail.textContent?.trim() || "";
    if (!text) {
      delete source.dataset.toastSignature;
      return;
    }

    const type = normalizeType(detail.dataset.toastType || source.dataset.toastType);
    const autohideValue =
      detail.dataset.toastAutohide ?? source.dataset.toastAutohide;
    const eventId = detail.dataset.toastEvent || "";
    const signature = `${type}\u0000${autohideValue ?? ""}\u0000${eventId}\u0000${text}`;
    if (source.dataset.toastSignature === signature) {
      return;
    }
    source.dataset.toastSignature = signature;
    showToast(text, {
      type,
      autohide: autohideValue === "false" ? false : undefined,
    });
    if (source.hasAttribute("data-toast-remove")) {
      source.remove();
    }
  }

  function sourcesFromMutation(record) {
    const sources = new Set();
    const target =
      record.target.nodeType === Node.ELEMENT_NODE
        ? record.target
        : record.target.parentElement;
    const owner = target?.closest?.(SOURCE_SELECTOR);
    if (owner) {
      sources.add(owner);
    }
    record.addedNodes.forEach((node) => {
      if (node.nodeType !== Node.ELEMENT_NODE) {
        return;
      }
      if (node.matches(SOURCE_SELECTOR)) {
        sources.add(node);
      }
      node.querySelectorAll(SOURCE_SELECTOR).forEach((source) => sources.add(source));
    });
    return sources;
  }

  function showExpiredSessionNotice() {
    if (window.location.pathname !== "/login") {
      return;
    }
    try {
      if (window.sessionStorage.getItem(SESSION_EXPIRED_KEY) !== "1") {
        return;
      }
      window.sessionStorage.removeItem(SESSION_EXPIRED_KEY);
      showToast("Sitzung abgelaufen. Bitte erneut anmelden.", { type: "info" });
    } catch (_error) {
      // Storage can be disabled without affecting the rest of the page.
    }
  }

  function initToasts() {
    document.querySelectorAll(SOURCE_SELECTOR).forEach(processSource);
    document.documentElement.classList.add("toasts-enhanced");
    const observer = new MutationObserver((records) => {
      const sources = new Set();
      records.forEach((record) => {
        sourcesFromMutation(record).forEach((source) => sources.add(source));
      });
      sources.forEach(processSource);
    });
    observer.observe(document.body, {
      childList: true,
      characterData: true,
      subtree: true,
    });
    showExpiredSessionNotice();
  }

  window.solarbankToasts = Object.freeze({ showToast });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initToasts, { once: true });
  } else {
    initToasts();
  }
})();
