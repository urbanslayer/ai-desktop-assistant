"""Spotify hub tools (music phase 1, docs/SPOTIFY_NOTES.md + docs/TOOLS.md D).

Six tools, all routed through the hub's SpotifyHub (hub/spotify.py): the hub
holds the token, enforces the dev-mode quota back-off and keeps the answers
human-readable (docs/ROUNDS.md: a tool result verifies the effect and says it
in one plain line). Nothing here streams audio — the Web API only controls
Spotify Connect devices; the desktop player is spotifyd under the device name
from settings (default "Riko").
"""
from __future__ import annotations

from .registry import ToolSpec

DESCRIPTIONS = {
    "spotify_play": (
        "Play music on Spotify: query is a song, artist, album or playlist "
        "name (or 'some <mood>'-style search words). kind: track (default) | "
        "album | playlist | artist. device: a Spotify Connect device name — "
        "omit it to use the desktop player named in Settings (default "
        "'Riko'). Resolves the top search hit and starts it; the result "
        "names what is now playing and where. Requires Spotify Premium."),
    "spotify_control": (
        "Control Spotify playback: action = pause | resume | next | prev | "
        "seek | volume | shuffle. value: seconds from track start for seek, "
        "0-100 for volume, true/false for shuffle (ignored otherwise)."),
    "spotify_now_playing": (
        "What Spotify is playing right now (track, artists, device, "
        "position) — the only honest answer to 'what is this song'."),
    "spotify_devices": (
        "List the online Spotify Connect devices [{name, id, type, active}] "
        "— check before naming a device the owner's Echo or phone happens to "
        "be called."),
    "spotify_transfer": (
        "Move Spotify playback to another Connect device by name "
        "(e.g. 'Echo Kitchen'); playback keeps going there."),
    "spotify_like_current": (
        "Like (save to the library) the track Spotify is playing right now."),
}

SPECS = (
    # name, risk, parameters, label(args)
    ("spotify_play", "act", {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "song / artist / album / playlist to play"},
            "kind": {"type": "string", "enum": ["track", "album", "playlist", "artist"]},
            "device": {"type": "string",
                       "description": "Connect device name (default: the "
                                      "device named in Settings, 'Riko')"},
        },
        "required": ["query"],
    }, lambda a: f"playing “{str((a or {}).get('query', ''))[:60]}” on Spotify"),
    ("spotify_control", "act", {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["pause", "resume", "next", "prev", "seek",
                                "volume", "shuffle"]},
            "value": {"description": "seconds (seek), 0-100 (volume), "
                                     "true/false (shuffle)"},
        },
        "required": ["action"],
    }, lambda a: f"Spotify: {str((a or {}).get('action', ''))[:16]}"),
    ("spotify_now_playing", "read", {"type": "object", "properties": {}},
     lambda a: "checking what's playing on Spotify"),
    ("spotify_devices", "read", {"type": "object", "properties": {}},
     lambda a: "listing Spotify devices"),
    ("spotify_transfer", "act", {
        "type": "object",
        "properties": {"device": {"type": "string",
                                  "description": "target device name"}},
        "required": ["device"],
    }, lambda a: f"moving Spotify to “{str((a or {}).get('device', ''))[:40]}”"),
    ("spotify_like_current", "act", {"type": "object", "properties": {}},
     lambda a: "liking the current track"),
)


def _fmt_pos(ms: int) -> str:
    secs = max(0, int(ms) // 1000)
    return f"{secs // 60}:{secs % 60:02d}"


def _fmt_track(snap: dict) -> str:
    who = f" by {snap['artists']}" if snap.get("artists") else ""
    return f"'{snap.get('track') or '?'}'{who}"


def build_spotify_specs(hub, tools_cfg: dict | None = None) -> list[ToolSpec]:
    """The six spotify_* specs bound to a SpotifyHub (hub/spotify.py).
    They are ALWAYS listed (permissions must stay editable while Spotify is
    off); `available` reflects enabled+configured, and every run answers in
    one plain human line or raises SpotifyError with one."""
    cfg = dict(tools_cfg or {})

    def _available() -> bool:
        try:
            return bool(hub.configured())
        except Exception:
            return False

    def play(args: dict):
        r = hub.play(str((args or {}).get("query") or ""),
                     str((args or {}).get("kind") or "track"),
                     str((args or {}).get("device") or ""))
        who = f" by {r['artists']}" if r.get("artists") else ""
        where = r["name"] + who
        if r["kind"] != "track":
            where = f"{r['kind']} “{r['name']}”"
        return f"Playing {where} on Spotify device “{r['device']}”."

    def control(args: dict):
        a = args or {}
        r = hub.control(str(a.get("action") or ""), a.get("value"))
        act = r.get("action")
        if act == "pause":
            return "Spotify paused."
        if act == "resume":
            return "Spotify is playing again."
        if act == "next":
            return "Skipped to the next Spotify track."
        if act == "prev":
            return "Went back to the previous Spotify track."
        if act == "seek":
            return f"Jumped to {_fmt_pos(r['position_ms'])} of the track."
        if act == "volume":
            return f"Spotify volume set to {r['volume_percent']}%."
        return f"Spotify shuffle {'on' if r['shuffle'] else 'off'}."

    def now_playing(args: dict):
        snap = hub.now_playing(force=True)
        if not snap.get("uri"):
            return "Nothing is playing on Spotify right now."
        state = "Playing" if snap.get("playing") else "Paused"
        pos = ""
        if snap.get("duration_ms"):
            pos = f" — {_fmt_pos(snap.get('position_ms', 0))} of "\
                  f"{_fmt_pos(snap['duration_ms'])}"
        return (f"{state} {_fmt_track(snap)} on “{snap.get('device') or '?'}”"
                f"{pos}.")

    def devices(args: dict):
        rows = hub.devices()
        if not rows:
            return ("No Spotify Connect device is online — start the desktop "
                    "player (tray: Music > Start player) or open Spotify "
                    "somewhere.")
        return ("Spotify devices: " + "; ".join(
            f"{d['name']} ({d['type']}{', NOW PLAYING' if d['active'] else ''})"
            for d in rows))

    def transfer(args: dict):
        r = hub.transfer(str((args or {}).get("device") or ""))
        return f"Spotify playback moved to “{r['device']}”."

    def like_current(args: dict):
        r = hub.like_current()
        return (f"Liked {_fmt_track(r)} — it is saved to the Spotify "
                "library.")

    runs = {"spotify_play": play, "spotify_control": control,
            "spotify_now_playing": now_playing, "spotify_devices": devices,
            "spotify_transfer": transfer, "spotify_like_current": like_current}
    specs = []
    for name, risk, params, label in SPECS:
        specs.append(ToolSpec(
            name=name, display=name, description=DESCRIPTIONS[name],
            parameters=params, source="hub", risk=risk,
            available=_available(),
            run=runs[name], label=label))
    del cfg
    return specs
