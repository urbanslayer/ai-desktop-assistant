"""Talk to Riko in the terminal: press Enter, speak, and she answers out loud on the avatar page.

    your voice -> asr.py -> text -> hub.chat -> her reply -> tts.py -> viewer.py -> avatar

Her brain, memory and personas live on the hub (see README); this app is
only ears, a mouth and the avatar page.

Run:  uv run main.py                 (uses asr / tts / hub from character_config.yaml)
      uv run main.py --tts fish      (override the voice)
      uv run main.py --asr remote    (override the ears)
      uv run main.py --type          (type to her instead of talking)

For the floating desktop avatar, run desktop.py instead.
"""
import argparse

from riko import asr, hub_client, tts, viewer
from riko.config import load_config
from riko.session import Session


def main():
    parser = argparse.ArgumentParser(description="Talk to Riko.")
    parser.add_argument("--asr", choices=["remote", "local", "groq"], help="speech to text (overrides the config)")
    parser.add_argument("--tts", choices=["hub", "none", "openai", "sovits", "fish"], help="her voice (overrides the config)")
    parser.add_argument("--type", action="store_true", help="type your messages instead of speaking")
    args = parser.parse_args()

    cfg = load_config()
    cfg["asr"] = args.asr or cfg["asr"]
    cfg["tts"] = args.tts or cfg["tts"]

    hub = hub_client.check(cfg)  # starts the hub in-process in local mode
    if not args.type:
        asr.check(cfg)
    tts.check(cfg)
    viewer.start(cfg)
    hub_client.start_state_poller()  # notice persona switches made on other clients
    session = Session(cfg, source="terminal")
    viewer.on_say(session.submit_text)  # the avatar page's input box feeds this too
    active = hub_client.active_persona() or {}
    print(f"\n=== Riko  |  hub: {hub.url}  |  persona: {active.get('name', '?')}"
          f"  |  ears: {'keyboard' if args.type else cfg['asr']}"
          f"  |  voice: {cfg['tts']} ===")
    print("Say 'switch to <persona>' (or just a keyword) to change who she is.\n")

    while True:
        try:
            if args.type:
                session.submit_text(input("\n> "))  # the session prints 'You:' once heard
            else:
                recording = asr.record()
                if recording is not None:
                    session.submit_audio(recording)
        except (KeyboardInterrupt, EOFError):
            print("\nBye!")
            return
        except Exception as e:  # one bad turn should not end the chat
            print(f"[error] {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
