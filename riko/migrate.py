"""Move an old chat_history.json onto the hub, once:  uv run -m riko.migrate [--prompt]

Before the hub, each app kept its own chat_history.json next to the code.
This imports it into the hub's currently-active persona (messages get
source 'import'), then renames the file to chat_history.json.imported, so a
second run can't import it twice.

  --prompt   also copy the old system_prompt from character_config.yaml into
             the active persona (the hub keeps personalities now). Without it
             the prompt is left alone; the run tells you it's still there.
"""
import json
import sys
from pathlib import Path

from riko import hub_client
from riko.config import ROOT, load_config

IMPORTED_SUFFIX = ".imported"


def _read_messages(path):
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"[migrate] {path.name} is not valid JSON: {e}")
    if not isinstance(history, list) or not history:
        sys.exit(f"[migrate] {path.name} is empty or not a list of messages — nothing to import.")
    messages = []
    for i, m in enumerate(history, 1):
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant") \
                or "content" not in m:
            sys.exit(f"[migrate] entry {i} in {path.name} is not a user/assistant message: "
                     f"{str(m)[:80]}")
        messages.append({"role": m["role"], "content": str(m["content"])})
    return messages


def main(argv=None, history_file=None, cfg=None, hub=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    path = Path(history_file) if history_file else ROOT / "chat_history.json"
    done = path.with_name(path.name + IMPORTED_SUFFIX)

    if done.exists():
        print(f"[migrate] Already done: {done.name} is here, this chat was imported once.")
        return 0
    if not path.exists():
        print(f"[migrate] No {path.name} next to the code — nothing to import.")
        return 0

    messages = _read_messages(path)
    cfg = cfg or load_config()
    hub = hub or hub_client.check(cfg)

    active = hub.state()["active"]
    n = hub.import_history(active["id"], messages)
    print(f"[migrate] imported {n} messages into persona '{active['name']}' ({active['id']})")

    old_prompt = str(cfg.get("system_prompt") or "").strip()
    if "--prompt" in argv:
        if not old_prompt:
            print("[migrate] --prompt: character_config.yaml has no system_prompt "
                  "(personalities live on the hub now).")
        else:
            p = hub.update_persona(active["id"], {"system_prompt": old_prompt})
            print(f"[migrate] old system_prompt copied into persona '{p['id']}' "
                  f"(tune it on the hub's web page)")
    elif old_prompt:
        print("[migrate] character_config.yaml still has a system_prompt — rerun with "
              "--prompt to copy it into the active persona, then remove it from the file.")

    path.replace(done)  # never overwrite: 'done exists' was checked up top
    print(f"[migrate] renamed {path.name} to {done.name}. Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
