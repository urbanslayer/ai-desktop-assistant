<picture>
  <source width="100%" srcset="./docs/banner-dark.png" media="(prefers-color-scheme: dark)" />
  <source width="100%" srcset="./docs/banner-light.png" media="(prefers-color-scheme: light), (prefers-color-scheme: no-preference)" />
  <img width="100%" src="./docs/banner-light.png" alt="Project Riko" />
</picture>

<h1 align="center">Project Riko</h1>

<p align="center">
  <b>Talk to her out loud. She talks back.</b><br />
  An anime AI companion that runs on your own PC, with her own voice and a 3D avatar that lip-syncs every word.
</p>

<p align="center">
  <a href="https://www.youtube.com/@JustRayen"><img src="https://img.shields.io/badge/YouTube-Just_Rayen-FF0000?style=for-the-badge&logo=youtube&logoColor=white" alt="YouTube: Just Rayen" /></a>
  <a href="https://patreon.com/RayenAI"><img src="https://img.shields.io/badge/Patreon-RayenAI-FF424D?style=for-the-badge&logo=patreon&logoColor=white" alt="Patreon: RayenAI" /></a>
  <a href="https://discord.gg/rayenai"><img src="https://img.shields.io/discord/1278117138909102170?style=for-the-badge&logo=discord&logoColor=white&label=Discord&color=5865F2" alt="Discord" /></a>
</p>
<p align="center">
  <img src="https://img.shields.io/badge/version-50K_snapshot-8A5CF6" alt="Version: 50K snapshot" />
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/uv-package_manager-DE5FE9?logo=uv&logoColor=white" alt="uv" />
  <img src="https://img.shields.io/badge/platform-Windows_%7C_Linux-555" alt="Windows | Linux" />
  <a href="LICENSE"><img src="https://img.shields.io/github/license/rayenfeng/riko_project?color=1fa669" alt="MIT license" /></a>
</p>

> [!IMPORTANT]
> **This is the 50K snapshot: a complete Riko, open source and yours to keep.** It's frozen at this release, so it stays a stable base to build on.
>
> Riko keeps growing on **[Patreon](https://patreon.com/RayenAI)**, home of the Full Source and the Desktop App. [See how they compare.](#three-ways)
>
> GitHub issues aren't monitored, so there's no support here.

## 🦊 Meet Riko

Riko is a voice companion you set up yourself. Press Enter and talk. She hears you, comes up with a reply (usually a snarky one), and says it out loud while her lips move along on screen.

This repo is the open-source core of Project Riko: the whole loop from your voice to hers, written to be read. It's under 500 lines in total, and every part sits in its own small file, so when you want to change how she thinks, sounds or looks, you know exactly where to go.

Her personality, her avatar and her voice all live in one config file. Making her yours takes a few minutes.

## ✨ What she can do

| | |
|---|---|
| 🎙️ **Listens** | Push-to-talk. Press Enter, speak, press Enter. Or just type to her. |
| 👂 **Understands** | Speech to text with [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper) on your own PC, on a second machine with a GPU (see [Remote ears](#remote-ears)), or [Groq](https://console.groq.com)'s fast online Whisper. |
| 🧠 **Thinks** | Any OpenAI-compatible server: a local one (vLLM, llama.cpp, LM Studio, Ollama) or OpenAI GPT itself, in whatever personality you write for her. |
| 📝 **Keeps the conversation** | The chat is saved to `chat_history.json`, so she picks up where you left off. |
| 🗣️ **Speaks** | Clones a voice from a short clip with [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS), uses any voice on [Fish Audio](https://fish.audio/?fpr=rayen10), or any OpenAI-style `/audio/speech` server. `tts: none` skips her voice and shows a speech bubble instead. |
| 💃 **Shows up** | A VRM avatar in your browser that lip-syncs to her voice. Drag to spin her around, right-drag to move her, scroll to zoom. |
| 🪟 **Lives on your desktop** | On Linux she can also run as a small borderless, transparent, always-on-top window, no browser involved: `uv run desktop.py` ([Desktop pet](#desktop-pet)). |
| 🎭 **Becomes anyone** | Personality, avatar and voice are all set in one file. |
| ✅ **Tells you what's missing** | Before she starts, a quick check catches a missing key, server or file and tells you how to fix it. |

## ⚙️ How it works

```mermaid
flowchart LR
    you(["🎙️ You talk"]) --> ears["👂 Ears<br/>Faster-Whisper (PC or server) or Groq"]
    ears --> brain["🧠 Brain<br/>any OpenAI-compatible LLM"]
    brain --> voice["🗣️ Voice<br/>GPT-SoVITS, Fish Audio or OpenAI-style"]
    voice --> face(["💃 Face<br/>VRM avatar + lip sync"])
```

Each step is one file, and `main.py` runs them in a loop:

| Step | File | What it does |
|---|---|---|
| 👂 Ears | `riko/asr.py` | Records you between the two Enters, turns it into text |
| 🧠 Brain | `riko/llm.py` | Sends the chat to your LLM server, saves her answer to the history |
| 🗣️ Voice | `riko/tts.py` | Turns her answer into a voice clip (or a plain bubble with `tts: none`) |
| 💃 Face | `riko/viewer.py` + `client/index.html` | Plays the clip on the avatar page and moves her mouth with it |

## 🚀 Quick start

The easiest way in uses Fish Audio for her voice: no GPU and nothing extra to install. If you'd rather generate her voice on your own PC, do this first and then see [Local voice](#local-voice).

**You need:** a Windows or Linux PC with a microphone, somewhere for her to think (an [OpenAI API key](https://platform.openai.com/api-keys), or any [OpenAI-compatible server](#brain) running locally), and a [Fish Audio API key](https://fish.audio/?fpr=rayen10) (sign up, then create one under *API keys*).

**1. Install uv.** It sets up Python for you.

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

```bash
# Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo apt install libportaudio2    # lets Python use your microphone
```

Close and reopen your terminal afterwards so it can find `uv`.

**2. Get the code.**

```bash
git clone https://github.com/rayenfeng/riko_project.git
cd riko_project
```

No git? Click the green **Code** button at the top of this page → **Download ZIP**, unzip it, then open a terminal in that folder. On Windows 11: right-click inside the folder → **Open in Terminal**.

**3. Install everything.**

```bash
uv sync
```

**4. Add your API keys.**

```bash
copy .env.example .env    # Linux: cp .env.example .env
notepad .env              # Linux: nano .env
```

Paste your keys after the `=` signs and save. `.env` is git-ignored, so your keys stay out of any commit.

**5. Start her.**

```bash
uv run main.py --tts fish
```

The first launch takes a minute while it downloads the speech recognition model.

**6. Say hi.**

1. Open **http://127.0.0.1:8080** and click once. Browsers only play sound after a click.
2. Back in the terminal, press **Enter**, say something, press **Enter**.
3. She answers on the page. **Ctrl+C** quits.

> [!TIP]
> Set `tts: fish` in `character_config.yaml` and a plain `uv run main.py` does the same thing.

## 🎨 Make her yours

Everything about her lives in `character_config.yaml`. Put her files (avatar, voice clip) in `character_files/`.

```yaml
system_prompt: |
  You are a helpful assistant named Riko.
  You speak like a snarky anime girl.
  Always refer to the user as "senpai".
```

| To change her... | Edit |
|---|---|
| **Personality** | `system_prompt`. Describe her the way you'd brief an actor: who she is, how she talks, what she calls you. |
| **Look** | `vrm_model`. Any `.vrm` file works. You can make your own for free in [VRoid Studio](https://vroid.com/en/studio). |
| **Fish voice** | `fish.voice_id`. Open any voice on [fish.audio](https://fish.audio/?fpr=rayen10) and copy the ID from the page address: `fish.audio/m/<voice id>`. |
| **GPT-SoVITS voice** | `ref_audio_path`: a 3–10 second clip with no long silences. `prompt_text`: exactly what the clip says. |
| **Brain** | The `llm:` block: `base_url` of any OpenAI-compatible server ([your own](#brain) or `https://api.openai.com/v1`), `model` (`auto` picks whatever the server offers), `api_key_env` (the `.env` variable holding the key, if any). |
| **Ears** | `asr: local` (Faster-Whisper, free, on your PC), `asr: remote` (Faster-Whisper on [another machine](#remote-ears)) or `asr: groq` (needs a free [Groq key](https://console.groq.com/keys)). |
| **Voice** | `tts: none` (bubble only), `openai` (any `/audio/speech` server, see `openai_tts:`), `sovits` or `fish`. |
| **Speech model size or GPU** | `local_asr.model` / `local_asr.device` in `character_config.yaml`. |
| **A fresh start** | Delete `chat_history.json`. |

You can also switch voice or ears for one run without touching the config:

```bash
uv run main.py --tts fish        # or sovits, openai, none
uv run main.py --asr groq        # or local, remote
uv run main.py --type            # type your messages instead of using a mic
```

<a id="brain"></a>
## 🧠 Bring your own brain

Her brain is any server that speaks the OpenAI chat API: vLLM, llama.cpp, LM Studio, Ollama in OpenAI mode, OpenAI itself. Point `llm.base_url` at it and she thinks there, nothing else changes:

```yaml
llm:
  base_url: http://192.0.2.51:8000/v1   # your server; https://api.openai.com/v1 for OpenAI
  api_key_env: LLM_API_KEY   # the .env variable holding the key; leave it empty if the server needs none
  model: auto                # "auto" = the first model the server lists at /v1/models
```

`model: auto` means you can swap the model on the server without touching Riko. Reasoning models spend part of their budget thinking, so `max_tokens` should leave headroom (1024 is a safe default).

<a id="remote-ears"></a>
## 👂 Remote ears

If your desktop PC has no GPU but another machine does, run her ears there. The microphone still records on your desktop; only the audio clip travels over your LAN.

On the GPU machine:

```bash
cd server
uv sync
uv run --extra cuda asr_server.py --device cuda   # NVIDIA (pulls the cuBLAS/cuDNN wheels)
uv run asr_server.py                              # or plain CPU
```

It listens on port 8001 with an OpenAI-compatible `/v1/audio/transcriptions` endpoint, so any OpenAI STT client works against it too. Set `ASR_API_KEY=secret` there to require a bearer token.

On the desktop PC set `asr: remote` in `character_config.yaml` and point `remote_asr.url` at that machine (put the same token in `.env` as `ASR_API_KEY` if you set one). If the ears server is down, Riko says so at startup and keeps working: voice fails, typing doesn't.

<details>
<summary>Local ears (no second machine)</summary>

`asr: local` runs Faster-Whisper on your own PC. Pick the model and device under `local_asr:` (`device: cuda` needs CUDA + cuDNN installed); the CPU runs fine with `base.en` or `small.en`.

</details>

<a id="local-voice"></a>
## 🎤 Local voice with GPT-SoVITS

Want her voice generated on your own machine, cloned from any clip you like? That's what GPT-SoVITS does. It runs best on an NVIDIA GPU and is installed separately.

1. Install [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) by following its guide.
2. In the GPT-SoVITS folder, start its API server and leave it running:
   ```bash
   python api_v2.py -a 127.0.0.1 -p 9880
   ```
3. Start Riko with `uv run main.py --tts sovits`.

She starts with the sample voice in `character_files/main_sample.wav`. Swap in your own clip (see the table above) to change it.

<a id="desktop-pet"></a>
## 🪟 Desktop pet (Linux)

Instead of a browser tab, she can live in a small borderless, transparent, always-on-top window on your desktop: `uv run desktop.py` (or `bin/riko-desktop`). It's the same page and the same loop, rendered by QtWebEngine, with the desktop showing through everywhere except her.

**In the window**

| Do | With |
|---|---|
| Move her around the screen | Left-drag anywhere (under Hyprland) |
| Pan / rotate / zoom the camera | Shift-drag / right-drag / scroll |
| Type to her | Press `/` or **Enter**, type, **Enter** to send, **Esc** to give up |
| Push-to-talk | Hold **Space**, speak, release |
| Quit | **Ctrl+Q**, the tray menu, or `riko-ctl quit` |

**From anywhere else:** `bin/riko-ctl <command>` talks to the running app over its local port — `type` (open her input and focus the window), `talk` / `talk-start` / `talk-stop`, `show` / `hide` / `toggle`, `status`, `forget`, `quit`. Handy in a keybinding or a script.

**Window rules.** `desktop.py` asks Hyprland to float, size and pin her on its own. For best results add the shipped rules so it happens the instant she maps:

```lua
-- Hyprland 0.55+ (Lua config): add to hyprland.lua, or copy into windowrules.lua / keybinds.lua
dofile(os.getenv("HOME") .. "/Development/riko_project/desktop/hyprland/riko.lua")
```

```ini
# classic hyprland.conf: source it and fix $RIKO
source = ~/Development/riko_project/desktop/hyprland/riko.conf
```

The lua file also wires global shortcuts (Super+Alt+T type, hold Super+Alt+R to talk, Super+Alt+H hide). Window size and bubble time are under `desktop:` in `character_config.yaml`.

To start her with the session, copy `desktop/riko.desktop` to `~/.config/autostart/` (or launch `bin/riko-desktop` from your compositor's autostart). Outside Hyprland she still appears, but floating/sizing is left to your window manager, and true transparency needs a compositor.

> [!NOTE]
> Under Hyprland, do the float+size as a window rule if you can. Mapping her tiled and resizing the window afterwards can leave a black strip at the bottom of the web view (a long-standing QtWebEngine/Wayland resize bug).

<a id="three-ways"></a>
## 💎 Three ways to have Riko

Same Riko, three ways to have her. Each one is made for a different kind of person, so pick the one that matches how you like to work.

| | 🛠️ Open Source<br /><sub>this repo</sub> | 🧪 [Full Source](https://patreon.com/RayenAI)<br /><sub>on Patreon</sub> | 🖥️ [Desktop App](https://patreon.com/RayenAI)<br /><sub>on Patreon</sub> |
|---|---|---|---|
| **Made for** | Builders who want a clean base to make their own | Tinkerers who want every new feature and the code behind it | Anyone who wants her up and running, no code involved |
| **What you get** | This repo: open source, under 500 lines | The full source of the newest build | A Windows app with an installer and a desktop icon |
| Talks with a lip-synced VRM avatar | ✅ | ✅ | ✅ |
| Hears you | Faster-Whisper on your PC or on a LAN server, or Groq | Groq Whisper | Groq Whisper |
| Her voice | GPT-SoVITS voice clone or Fish Audio | GPT-SoVITS voice clone or Fish Audio | Both, switched in Settings, with automatic fallback |
| Conversation | Push-to-talk, easy to rewire | Hands-free: she can tell when you've finished | Hands-free, or type to her |
| Face and body | Lip sync, ready for your own animation system | Expressions and animations she picks herself | Expressions and animations she picks herself |
| Memory | Chat history in a file you control | Long-term memory across sessions | Long-term memory you can browse, edit and reset |
| Doing things | Plug in your own tools | Tool calling with tools you write | Looks things up online when you ask |
| Backgrounds and anime scenes | A clean stage for your own | Drop in any 3D room, with lighting presets and a light editor | A clean stage, all eyes on her |
| Your own character | Edit one config file: avatar, voice, personality | Edit the config files: avatar, voice, personality, room | Guided in the app: drop in a VRM, pick a voice, write her personality |
| VR and desktop window | Transparent floating window on Linux (Hyprland) | Quest VR and a transparent floating window | Transparent floating window |
| Setup | `uv` and a terminal, with a setup checker | `setup.bat`, with a setup checker | One installer; keys tested on screen as you paste them |
| Runs on | Windows and Linux | Windows | Windows 10 / 11 |
| License | [MIT](LICENSE): fork it, change it, ship what you build | Personal use, full source included | Personal use; your videos and streams are yours |
| Updates | Stays at the 50K snapshot | New features as they land | Supported updates and bug fixes for version 1 |

<p align="center"><a href="https://patreon.com/RayenAI"><b>Get the Full Source or the Desktop App on Patreon →</b></a></p>

## 📋 Requirements

| | With Fish Audio (easiest) | With GPT-SoVITS (local voice) |
|---|---|---|
| **System** | Windows 10/11 or Linux | Windows 10/11 or Linux |
| **GPU** | Not needed | NVIDIA recommended |
| **Python** | 3.11+, installed by uv | 3.11+, installed by uv |
| **API keys** | LLM server key (`LLM_API_KEY`, or none for a local server) + Fish Audio | Same, no Fish key |
| **Also** | Microphone, speakers or headphones, a browser with WebGL, an internet connection | Same, plus a working GPT-SoVITS install |

Tested on Windows 11 and Ubuntu 22.04 (WSL2) with an RTX 3060 12 GB. Faster-Whisper runs on the CPU, so any modern processor handles the ears.

**Where the keys come from**

| Key | When | Get it |
|---|---|---|
| `LLM_API_KEY` | `llm.api_key_env` says so; leave empty if your server needs none | your server (or [platform.openai.com](https://platform.openai.com/api-keys) for OpenAI) |
| `ASR_API_KEY` | `asr: remote` and the ears server was started with a key | anything you choose, shared with `server/asr_server.py` |
| `TTS_API_KEY` | `tts: openai`, if that server needs a key | that server |
| `GROQ_API_KEY` | `asr: groq` | [console.groq.com](https://console.groq.com/keys) (free tier) |
| `FISH_API_KEY` | `tts: fish` | [fish.audio](https://fish.audio/?fpr=rayen10) |

## 🩹 Troubleshooting

| You see | Try this |
|---|---|
| `[setup] OPENAI_API_KEY is missing` (or another key) | Add it to `.env`. Check the file is called exactly `.env`, not `.env.txt`. |
| `GPT-SoVITS is not running` | Start its API server first (see [Local voice](#local-voice)), or run with `--tts fish`. |
| `Port 8080 is busy` | Riko is probably already running in another terminal. Close that one. |
| `Avatar not found` | Check the `vrm_model` path in `character_config.yaml`. |
| She replies in the terminal, but there's no sound | Click the avatar page once. Browsers block sound until you do. |
| `no avatar page open, playing on speakers` | That's normal: with no page open she talks through your speakers. Open the page to see her. |
| `(heard nothing, try again)` | Make sure your mic is the default recording device, and talk a little longer. |
| `remote ASR not reachable ...` | Start `server/asr_server.py` on the ears machine (see [Remote ears](#remote-ears)), or switch to `asr: local`. Typing keeps working either way. |
| Desktop window: a black strip along the bottom | Your compositor resized her after she mapped. Add the [window rules](#desktop-pet) so she starts floated and sized. |
| Desktop window: she looks opaque | You need a running Wayland compositor (or X11 compositor) for transparency. |

## 🔧 Under the hood

```
main.py                the loop: record → transcribe → reply → speak → play
desktop.py             the floating desktop window (PySide6 + QtWebEngine, Linux)
riko/
  config.py            loads character_config.yaml and .env
  asr.py               push-to-talk recording + speech to text (local / remote / groq)
  llm.py               LLM server reply + chat_history.json
  tts.py               GPT-SoVITS, Fish Audio or OpenAI-style TTS → client/audio/riko.wav
  viewer.py            serves the avatar page and hands it each voice clip
client/
  index.html           three.js + three-vrm: the avatar, lip sync, rotate and zoom
server/
  asr_server.py        Faster-Whisper as a small HTTP server (her remote ears)
bin/
  riko-ctl             command the running app from anywhere (status / type / talk / quit ...)
  riko-desktop         launcher for the desktop window
desktop/
  riko.desktop         launcher / autostart entry
  hyprland/            riko.lua + riko.conf: window rules and global shortcuts
character_files/       her avatar (sample_avatar.vrm) and sample voice (main_sample.wav)
character_config.yaml  her personality, brain, ears, voice and window
```

Every part also runs on its own, which helps when something isn't working:

```bash
uv run python -m riko.llm      # one reply from her LLM server
uv run python -m riko.tts      # one voice line (add --fish for Fish Audio)
uv run python -m riko.asr      # record, then print what she heard
```

**Ideas if you want to build on it**

- **Hands-free listening.** Replace `record()` in `riko/asr.py` with something that notices when you stop talking.
- **Emotions.** `client/index.html` already opens her mouth with `vrm.expressionManager.setValue('aa', ...)`. Expressions like `happy` and `angry` work the same way.

## 🙏 Credits

- Voice by [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) and [Fish Audio](https://fish.audio/?fpr=rayen10)
- Speech recognition by [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper) and [Groq](https://groq.com)
- Language model by [OpenAI](https://platform.openai.com)
- Avatar rendering by [three.js](https://threejs.org) and [three-vrm](https://github.com/pixiv/three-vrm)
- Sample avatar by [pixiv Inc.](https://github.com/pixiv/three-vrm) (VRM1_Constraint_Twist_Sample)

## 📄 License

[MIT](LICENSE). Feel free to clone, modify, and build your own waifu voice companion.
