/* Riko Helper — risk classification, pure functions with no browser APIs so
 * they can be unit-tested with `node --test`.
 *
 * docs/ROUNDS.md R2-F: anything that submits a form, or whose label/form
 * looks like send/buy/pay/order/post/publish/delete/remove/confirm/sign in,
 * is upgraded to danger and always asks, whatever the setting says.
 */
(function (root) {
  "use strict";
  if (root.RikoRisk) return;  // idempotent: scripting.executeScript may re-run us

  // Words that mean "this click has consequences outside the page".
  const DANGER_WORDS = [
    "send", "buy", "pay", "order", "post", "publish", "delete", "remove",
    "confirm", "sign in", "signin", "sign-in", "log in", "login", "log-in",
    "purchase", "checkout", "check out", "submit", "place order",
  ];
  const DANGER_RE = new RegExp(
    "\\b(" + DANGER_WORDS.map((w) => w.replace(/[ /-]/g, "[ /_-]?")).join("|") + ")\\b", "i");

  // Buttons inside a <form> that submits something (heuristic: submit-like
  // labels, or a form whose action isn't "#").
  const FORM_SUBMIT_RE = /\b(send|submit|search|go|sign|log ?in|register|buy|pay|order|post|publish)\b/i;

  /** True when any danger word appears in the element's visible text/labels. */
  function looksDangerous(text) {
    return !!text && DANGER_RE.test(String(text));
  }

  /** Words matched inside `text` (for the approval question). */
  function dangerWords(text) {
    const found = new Set();
    const re = new RegExp(DANGER_RE.source, "gi");
    for (const m of String(text || "").matchAll(re)) found.add(m[0].toLowerCase());
    return [...found];
  }

  /**
   * classify(method, params, element) -> {risk, reason}
   *   method  : "click" | "type" | "select" | "scroll" | ...
   *   params  : the tool arguments ({submit: true} matters)
   *   element : what the snapshot saw, plain JSON:
   *             {tag, text, ariaLabel, title, placeholder, value, href,
   *              inputType, formText, formAction}
   * Read-only methods are always read. Clicks/types on ordinary things are
   * "act"; the classifier only ever upgrades, never downgrades.
   */
  function classify(method, params, element) {
    params = params || {};
    const el = element || {};
    if (["scroll", "back", "screenshot", "read", "tabs", "open", "close"].includes(method)) {
      return { risk: "read", reason: "" };
    }

    if (method === "type" && params.submit) {
      // Enter in a field submits whatever form it lives in — always ask.
      return { risk: "danger", reason: `type "${params.text ?? ""}" and press Enter (this submits)` };
    }

    const label = [el.text, el.ariaLabel, el.title, el.placeholder, el.value]
      .filter(Boolean).join(" | ");
    const words = dangerWords(label);
    const inForm = !!el.formAction && FORM_SUBMIT_RE.test(
      [el.formText, el.formAction, el.text].filter(Boolean).join(" "));

    if (method === "click" || method === "type" || method === "select") {
      if (words.length) {
        const what = method === "type" ? `type into "${shorten(label)}" — its label says ${words.join("/")}`
          : method === "select" ? `pick an option in "${shorten(label)}"`
          : `click "${shorten(el.text || el.ariaLabel || el.value || label)}" — its label says ${words.join("/")}`;
        return { risk: "danger", reason: what };
      }
      if (method === "click" && inForm) {
        return { risk: "danger", reason: `click "${shorten(label)}" inside a form that submits` };
      }
      if (method === "click" && el.href && /^(javascript|mailto|tel):/i.test(el.href)) {
        return { risk: "danger", reason: `follow ${el.href.split(":")[0]} link "${shorten(label)}"` };
      }
      if (method === "type") {
        return { risk: "act", reason: "" };
      }
      return { risk: method === "click" ? "act" : "act", reason: "" };
    }
    return { risk: "act", reason: "" };
  }

  function shorten(text, max = 60) {
    const flat = String(text || "").replace(/\s+/g, " ").trim();
    return flat.length > max ? flat.slice(0, max - 1) + "…" : flat;
  }

  const api = { DANGER_WORDS, looksDangerous, dangerWords, classify, shorten };
  root.RikoRisk = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
