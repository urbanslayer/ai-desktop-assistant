# Plan: tools, internet access and calling other models

Status: **plan, not started.** Section C (other models) is **plan only, do not implement** (owner's instruction).
The open questions at the end need the owner's answer before work starts.

## Facts checked

- The current LLM (`qwen4exp` on 192.0.2.51:8000, vLLM) **already does OpenAI-style tool calling**: given a
  `web_search` tool and asked about the weather, it returned a proper `tool_calls` entry with a query.
- The owner's **SearXNG** on Nemu (192.0.2.101:8989) answers `format=json` searches: a free, private
  search backend that's already running.
- Maintained building blocks: MCP Python SDK (`modelcontextprotocol/python-sdk`), Microsoft
  `playwright-mcp` (browser automation over MCP), `ChromeDevTools/chrome-devtools-mcp` (drives a real Chrome).
  All of them were active this week.

## A. Where tools run: hub tools vs. client tools

The hub is the brain, so the **tool loop runs in the hub**. Every client (desktop, web, Android later)
gets the same assistant. Tools come in two kinds:

| kind | runs on | examples |
|---|---|---|
| **hub tools** | the hub box (eve) | `web_search` (SearXNG), `fetch_page` (download + readable text), `get_time`, `calculator`, `remember/recall` (long-term notes), any **MCP server** configured on the hub |
| **client tools** | the device you're using (your PC) | `open_url` (opens in your normal browser via `xdg-open` / the desktop portal), `browser.*` (drive your browser, see B), `clipboard_read/write`, `notify`, later `open_app`, `read_file` from allowed folders |

**How client tools reach your PC:** the desktop app keeps a long-poll (or WebSocket) channel open to the hub and
**registers its capabilities** (the list of client tools it offers, with JSON schemas). When the model calls one,
the hub sends a `tool_request` down that channel, the app runs it (asking you first when the policy says so) and
posts the result back. The loop then continues. If no client with that tool is online, the tool reports
"not available right now". The Android app can later register its own tools (open a link on the phone, share,
set an alarm) the same way.

**The loop:** model → tool calls (run in parallel where safe) → results → model, at most N steps
(default 8) and T seconds (default 90). Progress events go out on the state channel ("searching the web for …",
"reading 3 pages") so the avatar plays its `think` animation and can say a short "let me look that up" line.
The final spoken answer is kept short, and the full text and links show in the bubble, the web page and the
history.

**Settings (web page):** tools on/off, the list of tools with per-tool on/off, **permission per tool**
(`always`, `ask me`, `never`), allowed folders, max steps/time, the SearXNG URL, the MCP servers list (command or
URL plus env), plus a **tool log** showing every call, its arguments, result size and who approved it.

## B. "Use my browser" — options

1. **Open links only** (`open_url`): opens in your default browser. Safe, simple, no setup.
2. **Drive your real browser** (recommended for "act as a full assistant"): the desktop app starts or attaches to
   **your Chrome/Chromium/Brave with remote debugging** on localhost and uses **chrome-devtools-mcp** (or
   Playwright over CDP) as a client tool: open tab, read page, click, type, fill forms, screenshot for
   the model.
   - **Upside:** it uses your logins.
   - **Downside:** it can do anything you can, so risky actions need your approval.
   - **Firefox:** only partial support (WebDriver BiDi). A dedicated Chromium profile is the dependable option.
3. **Separate assistant browser:** Playwright with its own profile (on your PC, or headless on eve). It's isolated
   from your logins, so it's safer but can't use your accounts.

**Safety rules (all options):**
- Web page text and tool results go into the model marked as untrusted data. They can't change her instructions
  or permissions (prompt-injection defence).
- Anything that sends, buys, posts, deletes, logs in or submits a form needs your OK by default (tray
  notification or the web page, with a spoken "can I…?").
- Domain allow/deny lists.
- Keys and passwords never pass through the model. Logins happen in the browser with your own sessions.
- Everything is logged on the hub.

## C. Letting her call other models (plan only — do not implement)

Goal: the core model (local qwen) stays the personality and the router, and can hand a task to a stronger or
special model when it needs to: OpenAI GPT models, Anthropic Claude, Gemini, OpenRouter, or other local models
(a coder model, a vision model).

- **As a tool:** `ask_model(model, task, context?, attachments?)`. The core model writes the sub-task and gets
  back text (plus any tool results), then answers in her own voice. A cheaper variant, `route`, lets the hub
  send the whole turn to another model and re-voice its answer through the persona.
- **Providers registry** in hub settings: name, kind (`openai-compatible`, `anthropic`, `gemini`), base URL,
  write-only API key (same blank-means-default rules as the LLM key), model list, and per-model notes the
  core model sees ("claude-…: best for long reasoning and code; costs $$"). OpenAI-compatible covers OpenAI,
  OpenRouter, vLLM, LM Studio and Ollama. Anthropic and Gemini get small native adapters in the hub, or a
  LiteLLM-style unified client (decide at build time after checking its maintenance and licence).
- **Guardrails:** monthly and per-call spend caps per provider, with a token count and estimated cost logged
  per call; `ask me` permission for paid providers by default; a privacy flag per provider (never send chat
  history, only the sub-task); timeouts; and fallbacks when a provider is down.
- **Sub-model tools:** off by default. A delegated model only gets the hub tools that are explicitly allowed for
  it, and never client tools without your approval.
- **API surface for clients:** nothing new except progress events ("asking Claude…"). History stores which model
  answered.
- **What's needed to build it later:** provider adapters, the settings UI, spend tracking, a tool schema, tests
  with fake providers, docs.

## Work split (when approved)

- **T1** hub tool loop + tool registry + permissions + tool log + `web_search` (SearXNG) + `fetch_page` +
  `get_time`, plus the progress events.
- **T2** client-tool channel (register / long-poll / result) + desktop `open_url` + approval prompts (tray
  notification + bubble).
- **T3** browser control client tool (option chosen below) + safety rules.
- **T4** MCP client in the hub (stdio and HTTP servers from settings).
- **C**: delegation to other models, held back until the owner says go.
- T1 and T2 can run in parallel. T3 and T4 come after.

## Open questions for the owner

1. Browser: drive your real browser with your logins (needs approvals for risky actions), a separate assistant
   browser, or open links only?
2. Which browser do you use day to day (Chrome, Chromium, Brave, Firefox)?
3. Default permission for actions that change things (send, buy, post, delete): always ask (recommended), or
   allow some per site?
4. Web search: your SearXNG on Nemu (recommended, private, free), or a paid API (Brave, Tavily)?
