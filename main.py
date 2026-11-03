"""Talk to Riko in the terminal: press Enter, speak, and she answers out loud on the avatar page.

    your voice -> asr.py -> text -> llm.py -> her reply -> tts.py -> her voice -> viewer.py -> avatar

Run:  uv run main.py                 (uses asr / tts from character_config.yaml)
      uv run main.py --tts fish      (override the voice)
      uv run main.py --asr remote    (override the ears)
      uv run main.py --type          (type to her instead of talking)

For the floating desktop avatar, run desktop.py instead.
"""
import argparse

from riko import asr, llm, tts, viewer
from riko.config import load_config


def main():
    parser = argparse.ArgumentParser(description="Talk to Riko.")
    parser.add_argument("--asr", choices=["remote", "local", "groq"], help="speech to text (overrides the config)")
    parser.add_argument("--tts", choices=["none", "openai", "sovits", "fish"], help="her voice (overrides the config)")
    parser.add_argument("--type", action="store_true", help="type your messages instead of speaking")
    args = parser.parse_args()

    cfg = load_config()
    cfg["asr"] = args.asr or cfg["asr"]
    cfg["tts"] = args.tts or cfg["tts"]

    llm.check(cfg)
    if not args.type:
        asr.check(cfg)
    tts.check(cfg)
    viewer.start(cfg)
    print(f"\n=== Riko  |  model: {llm.model_name()}  |  ears: {'keyboard' if args.type else cfg['asr']}"
          f"  |  voice: {cfg['tts']} ===")

    while True:
        try:
            if args.type:
                text = input("\nYou:  ").strip()
            else:
                recording = asr.record()
                if recording is None:
                    continue
                text = asr.transcribe(recording, cfg)
                if text:
                    print(f"You:  {text}")
            if not text:
                print("(heard nothing, try again)")
                continue
            answer = llm.reply(text, cfg)
            print(f"Riko: {answer}")
            viewer.play(tts.speak(answer, cfg), answer)
        except (KeyboardInterrupt, EOFError):
            print("\nBye!")
            return
        except Exception as e:  # one bad turn should not end the chat
            print(f"[error] {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
