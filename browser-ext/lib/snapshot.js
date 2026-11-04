/* Riko Helper — snapshot building, pure (no DOM, no browser APIs) so
 * `node --test` can check the numbering and the password rules.
 *
 * The content script walks the page and produces plain element descriptors;
 * this file turns them into the numbered, serialisable snapshot the model
 * reads (docs/ROUNDS.md R2-F: numbered interactive elements, readable text,
 * never password values).
 */
(function (root) {
  "use strict";

  const MAX_TEXT = 8000;        // readable text cap per read
  const MAX_ELEMENTS = 120;     // interactive elements per read
  const MAX_LABEL = 160;

  const INTERACTIVE_TAGS = new Set(["A", "BUTTON", "INPUT", "TEXTAREA", "SELECT", "SUMMARY"]);
  const INTERACTIVE_ROLES = new Set(["button", "link", "textbox", "combobox", "checkbox",
    "radio", "tab", "menuitem", "searchbox", "switch", "slider", "option"]);

  /** Should this element be offered as a numbered thing to click? */
  function isInteractive(tag, role, clickableHandlers) {
    if (INTERACTIVE_TAGS.has(String(tag || "").toUpperCase())) return true;
    if (INTERACTIVE_ROLES.has(String(role || "").toLowerCase())) return true;
    return !!clickableHandlers;
  }

  /**
   * buildSnapshot({title, url, text, elements}) -> plain JSON for the wire.
   * `elements` are the raw descriptors from the page ({tag, role, text,
   * ariaLabel, title, placeholder, value, href, inputType, disabled, hidden,
   * formText, formAction}); ids come from the content script's data-riko-n.
   * Numbering is 1-based and stable for the snapshot; password values are
   * never included, and hidden elements are skipped.
   */
  function buildSnapshot(input) {
    input = input || {};
    const seen = new Map(); // dedupe by identity string
    const elements = [];
    let n = 0;
    for (const el of (input.elements || [])) {
      if (!el || typeof el !== "object" || el.hidden) continue;
      const key = el.id != null ? `#${el.id}` : JSON.stringify(el);
      if (seen.has(key)) continue;
      seen.set(key, true);
      const isPassword = String(el.inputType || "").toLowerCase() === "password";
      const label = firstText(el.text, el.ariaLabel, el.title, el.placeholder);
      const entry = {
        n: ++n,
        tag: String(el.tag || "").toLowerCase(),
        text: clamp(label, MAX_LABEL),
      };
      if (el.role) entry.role = el.role;
      if (el.href) entry.href = clamp(el.href, MAX_LABEL);
      if (el.inputType) entry.input_type = el.inputType;
      if (isPassword) {
        entry.value = el.value ? "••••••" : "";  // never the real value
      } else if (el.value != null && el.value !== "") {
        entry.value = clamp(String(el.value), MAX_LABEL);
      }
      if (el.name) entry.name = clamp(el.name, 60);
      if (el.disabled) entry.disabled = true;
      if (el.id != null) entry.id = el.id;  // content-script handle for click/type
      elements.push(entry);
      if (n >= MAX_ELEMENTS) break;
    }
    return {
      title: clamp(String(input.title || ""), 200),
      url: String(input.url || ""),
      text: clamp(String(input.text || ""), MAX_TEXT),
      elements,
      truncated: (input.text || "").length > MAX_TEXT || (input.elements || []).length > MAX_ELEMENTS,
    };
  }

  function firstText(...parts) {
    for (const p of parts) if (p && String(p).trim()) return String(p).trim();
    return "";
  }

  function clamp(text, max) {
    text = String(text ?? "");
    return text.length > max ? text.slice(0, max - 1) + "…" : text;
  }

  const api = { buildSnapshot, isInteractive, MAX_TEXT, MAX_ELEMENTS, INTERACTIVE_TAGS };
  root.RikoSnapshot = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
