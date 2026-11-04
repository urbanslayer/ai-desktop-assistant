/* Riko Helper — content script: the only part of her that touches your DOM.
 *
 * The background page relays commands here as runtime messages:
 *   {cmd:"snapshot"}                                  -> a numbered snapshot
 *   {cmd:"act", kind:"click"|"type"|"select", ...}    -> do a thing, maybe ask
 *   {cmd:"scroll", direction, amount}
 *   {cmd:"banner", on:true|false}
 * Password values are never read or typed, and anything the risk classifier
 * calls `danger` answers needs_confirmation unless the request already came
 * back confirmed (the owner said yes).
 */
"use strict";

(() => {
  const BANNER_ID = "riko-helper-banner";
  const ID_ATTR = "data-riko-id";
  let idSeq = 0;
  let bannerTimer = null;

  const isHidden = (el) => {
    if (el.tagName === "INPUT" && el.type === "hidden") return true;
    if (el.getAttribute("aria-hidden") === "true" && !el.hasAttribute(ID_ATTR)) return true;
    if (!el.getClientRects().length) return true;
    const style = getComputedStyle(el);
    return style.visibility === "hidden" || style.display === "none";
  };

  const labelFor = (el) => {
    const texts = [];
    if (el.labels && el.labels.length) texts.push(...[...el.labels].map((l) => l.innerText));
    texts.push(el.innerText, el.value, el.getAttribute("aria-label"),
               el.getAttribute("title"), el.getAttribute("placeholder"), el.name);
    for (const t of texts) if (t && String(t).trim()) return String(t);
    return "";
  };

  const descriptor = (el, id) => {
    const tag = el.tagName;
    const href = el.tagName === "A" ? el.href
      : (el.closest && el.closest("a") ? el.closest("a").href : "");
    const form = el.form || null;
    const d = {
      id,
      tag,
      role: el.getAttribute("role") || "",
      text: labelFor(el),
      ariaLabel: el.getAttribute("aria-label") || "",
      title: el.getAttribute("title") || "",
      placeholder: el.getAttribute("placeholder") || "",
      href,
      hidden: isHidden(el),
    };
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
      d.inputType = tag === "SELECT" ? "select" : (el.type || "text");
      d.value = el.value || "";
      d.name = el.name || "";
    }
    if (el.disabled) d.disabled = true;
    if (form) {
      d.formAction = form.getAttribute("action") || "(same page)";
      d.formText = [...form.querySelectorAll("button, input[type=submit]")]
        .map((b) => b.innerText || b.value).join(" ");
    }
    return d;
  };

  const interactiveElements = () => {
    const sel = "a,button,input,textarea,select,summary,[role],[contenteditable='true']";
    const out = [];
    for (const el of document.querySelectorAll(sel)) {
      const role = el.getAttribute("role") || "";
      if (!RikoSnapshot.isInteractive(el.tagName, role, false)) continue;
      if (el.getAttribute("role") === "presentation" || el.getAttribute("role") === "none") continue;
      let id = el.getAttribute(ID_ATTR);
      if (id == null) { id = String(++idSeq); el.setAttribute(ID_ATTR, id); }
      out.push({ el, id });
    }
    return out;
  };

  const readableText = () => {
    // body.innerText skips hidden nodes and script/style text; it is the
    // cheap "reader mode" good enough for a snapshot.
    const t = document.body ? document.body.innerText : "";
    return t.replace(/\n{3,}/g, "\n\n");
  };

  const snapshot = () => RikoSnapshot.buildSnapshot({
    title: document.title,
    url: location.href,
    text: readableText(),
    elements: interactiveElements().map(({ el, id }) => descriptor(el, id)),
  });

  const findElement = (id) => document.querySelector(`[${ID_ATTR}="${CSS.escape(String(id))}"]`);

  const setNativeValue = (el, value) => {
    const proto = el.tagName === "INPUT" ? HTMLInputElement.prototype
      : el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype
      : HTMLSelectElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  };

  const banner = (on) => {
    const old = document.getElementById(BANNER_ID);
    if (bannerTimer) { clearTimeout(bannerTimer); bannerTimer = null; }
    if (!on) { if (old) old.remove(); return; }
    if (!old) {
      const div = document.createElement("div");
      div.id = BANNER_ID;
      div.textContent = "🤖 Riko is driving — click the puzzle-piece icon to pause her";
      div.setAttribute("style", "position:fixed;top:0;left:0;right:0;z-index:2147483647;"
        + "background:#ffb020;color:#241a02;font:14px/1.4 system-ui,sans-serif;"
        + "padding:6px 12px;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.3)");
      (document.body || document.documentElement).appendChild(div);
    }
    bannerTimer = setTimeout(() => banner(false), 30000);  // safety: never stuck on
  };

  const act = (msg) => {
    const el = findElement(msg.id);
    if (!el) return { ok: false, error: "element " + msg.n + " is gone (the page changed — read it again)" };
    const desc = descriptor(el, msg.id);
    desc.text = labelFor(el);

    const isPassword = desc.inputType === "password";
    if (isPassword && msg.kind === "type") {
      return { ok: false, error: "that is a password field — Riko never types passwords" };
    }
    if (isPassword && msg.kind === "click" && !msg.confirmed) {
      return { ok: false, error: "that is a password field" };
    }

    const verdict = RikoRisk.classify(msg.kind === "click" ? "click" : msg.kind,
                                      { text: msg.text, submit: msg.submit }, desc);
    if (verdict.risk === "danger" && !msg.confirmed) {
      return { ok: false, needs_confirmation: true,
               reason: `${verdict.reason} on ${location.host}` };
    }

    if (msg.kind === "click") {
      el.focus();
      el.click();
      return { ok: true, result: `clicked ${RikoRisk.shorten(desc.text || desc.tag)} on ${location.host}` };
    }
    if (msg.kind === "type") {
      el.focus();
      setNativeValue(el, String(msg.text ?? ""));
      if (msg.submit) {
        if (el.form && typeof el.form.requestSubmit === "function") el.form.requestSubmit();
        else el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      }
      return { ok: true, result: `typed into ${RikoRisk.shorten(desc.text || desc.tag)} on ${location.host}` };
    }
    if (msg.kind === "select") {
      const want = String(msg.value ?? "").toLowerCase();
      const opt = [...el.options].find((o) => o.value.toLowerCase() === want)
        || [...el.options].find((o) => (o.text || "").toLowerCase().includes(want))
        || [...el.options].find((o) => want.includes((o.text || "").toLowerCase()) && o.text);
      if (!opt) {
        return { ok: false, error: `no option matching ${JSON.stringify(msg.value)} in that select` };
      }
      el.focus();
      setNativeValue(el, opt.value);
      return { ok: true, result: `selected ${RikoRisk.shorten(opt.text)} on ${location.host}` };
    }
    return { ok: false, error: `unknown action ${msg.kind}` };
  };

  const scroll = (msg) => {
    const amount = Math.max(50, Math.min(20000, msg.amount || Math.round(innerHeight * 0.9)));
    if (msg.direction === "top") scrollTo(0, 0);
    else if (msg.direction === "bottom") scrollTo(0, document.body.scrollHeight);
    else scrollBy(0, msg.direction === "up" ? -amount : amount);
    return { ok: true, result: `scrolled ${msg.direction} on ${location.host}` };
  };

  browser.runtime.onMessage.addListener((msg) => {
    if (!msg || typeof msg.cmd !== "string") return;
    if (!["snapshot", "act", "scroll", "banner"].includes(msg.cmd)) return;
    if (msg.cmd === "banner") { banner(!!msg.on); return; }  // fire and forget
    if (msg.cmd === "snapshot") return Promise.resolve(snapshot());
    if (msg.cmd === "scroll") return Promise.resolve(scroll(msg));
    return Promise.resolve(act(msg));
  });
})();
