// Riko Helper — the risk classifier must catch every "this has consequences"
// button and never downgrade anything. Run: node --test browser-ext/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const RikoRisk = require("../lib/risk.js");
const { classify, looksDangerous, dangerWords, shorten } = RikoRisk;

const click = (el, params = {}) => classify("click", params, el);

test("plain links and buttons are just act", () => {
  assert.equal(click({ tag: "A", text: "Home", href: "https://a.dev/" }).risk, "act");
  assert.equal(click({ tag: "BUTTON", text: "Show comments" }).risk, "act");
  assert.equal(classify("scroll", {}, {}).risk, "read");
  assert.equal(classify("read", {}, {}).risk, "read");
});

test("send/buy/pay/order/post/publish/delete/remove/confirm/sign in all upgrade", () => {
  const risky = ["Send", "Buy now", "Pay €12", "Place order", "Post reply",
                 "Publish", "Delete account", "Remove from cart", "Confirm",
                 "Sign in", "Log in", "Login", "Checkout", "Submit", "Purchase"];
  for (const text of risky) {
    const v = click({ tag: "BUTTON", text });
    assert.equal(v.risk, "danger", `${text} should be danger`);
    assert.ok(v.reason.includes(text.toLowerCase().split(" ")[0]) || v.reason.length > 0);
  }
});

test("danger words are word-boundary matched, not substring noise", () => {
  assert.equal(looksDangerous("sended"), false);
  assert.equal(looksDangerous("Buyer's guide"), false);   // "Buyer" is not "buy"
  assert.equal(looksDangerous("feedback"), false);        // no 'back' word anyway
  assert.equal(looksDangerous("Post (publish)"), true);
  assert.deepEqual(dangerWords("Buy and send"), ["buy", "send"]);
});

test("aria-label and placeholder count as labels", () => {
  assert.equal(click({ tag: "I", ariaLabel: "Delete item" }).risk, "danger");
  assert.equal(click({ tag: "SPAN", placeholder: "", text: "", ariaLabel: "Confirm order" }).risk, "danger");
});

test("a click inside a submitting form is danger even with a tame label", () => {
  const v = click({ tag: "BUTTON", text: "OK", formAction: "/checkout",
                    formText: "Place order" });
  assert.equal(v.risk, "danger");
});

test("javascript:/mailto:/tel: links are danger", () => {
  assert.equal(click({ tag: "A", text: "Mail us", href: "mailto:a@b.c" }).risk, "danger");
  assert.equal(click({ tag: "A", text: "Do it", href: "javascript:go()" }).risk, "danger");
});

test("type + submit is always danger (it submits whatever form)", () => {
  assert.equal(classify("type", { text: "hi", submit: true }, { tag: "INPUT" }).risk, "danger");
  assert.equal(classify("type", { text: "hi" }, { tag: "INPUT" }).risk, "act");
});

test("typing into a 'Send' field is danger too", () => {
  const v = classify("type", { text: "x" }, { tag: "TEXTAREA", placeholder: "Message to send" });
  assert.equal(v.risk, "danger");
});

test("classify never throws on junk input", () => {
  for (const junk of [undefined, null, {}, { tag: 42, text: null }, "weird"]) {
    assert.ok(["read", "act", "danger"].includes(classify("click", {}, junk).risk));
  }
});

test("shorten flattens and truncates labels", () => {
  assert.equal(shorten("  a\n\n  b  "), "a b");
  assert.equal(shorten("x".repeat(200), 60).length, 60);
});
