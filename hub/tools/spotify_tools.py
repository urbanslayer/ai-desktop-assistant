"""Spotify hub tools (music phase 1, docs/SPOTIFY_NOTES.md + docs/TOOLS.md D).

Seven tools, all routed through the hub's SpotifyHub (hub/spotify.py): the hub
holds the token, enforces the dev-mode quota back-off and keeps the answers
human-readable (docs/ROUNDS.md: a tool result verifies the effect and says it
in one plain line). Nothing here streams audio — the Web API only controls
Spotify Connect devices; the desktop player is spotifyd under the device name
from settings (default "Riko"). Owner playlists/liked songs come from
GET /me/playlists and /me/tracks — catalogue /search does not reliably
surface the owner's own (private) playlists.
"""
from __future__ import annotations

try:                                   # package mode (uv run python -m hub.server)
    from ..spotify import normalise_name
except ImportError:                    # standalone mode (`python server.py` in hub/)
    from spotify import normalise_name  # type: ignore[no-redef]
from .registry import ToolSpec

DESCRIPTIONS = {
    "spotify_play": (
        "Play music on Spotify: query is a song, artist, album or playlist "
        "name (or 'some <mood>'-style search words). kind: track (default) | "
        "album | playlist | artist. For the owner's own playlists (\\\"my X "
        "playlist\\\") use kind=playlist — the owner's library is searched "
        "first (catalogue search misses private playlists). 'my liked "
        "songs'/'saved tracks' plays the saved songs. shuffle: true shuffles "
        "(used for liked songs). device: a Spotify Connect device name — "
        "omit it to use the desktop player named in Settings (default "
        "'Riko'). Resolves the best hit and starts it; the result names "
        "what is now playing and where. Requires Spotify Premium. A "
        "spotify:<type>:<id> URI or open.spotify.com link plays DIRECTLY "
        "(no search)."),
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
    "spotify_playlists": (
        "List the owner's own Spotify playlists by name with track counts — "
        "the answer to 'what playlists do I have?'. query (optional) filters "
        "by name. To play one use spotify_play with kind=playlist — the "
        "owner's library is searched first."),
}

SPECS = (
    # name, risk, parameters, label(args)
    ("spotify_play", "act", {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "song / artist / album / playlist to play "
                                     "('my liked songs' plays the saved tracks)"},
            "kind": {"type": "string", "enum": ["track", "album", "playlist", "artist"]},
            "device": {"type": "string",
                       "description": "Connect device name (default: the "
                                      "device named in Settings, 'Riko')"},
            "shuffle": {"type": "boolean",
                        "description": "shuffle the queue (for 'play my "
                                       "liked songs shuffled')"},
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
    ("spotify_playlists", "read", {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "optional name filter"},
        },
    }, lambda a: (f"listing Spotify playlists matching "
                  f"“{str((a or {}).get('query', ''))[:40]}”"
                  if (a or {}).get("query") else "listing Spotify playlists")),
)


def _fmt_pos(ms: int) -> str:
    secs = max(0, int(ms) // 1000)
    return f"{secs // 60}:{secs % 60:02d}"


def _fmt_track(snap: dict) -> str:
    who = f" by {snap['artists']}" if snap.get("artists") else ""
    return f"'{snap.get('track') or '?'}'{who}"


def build_spotify_specs(hub, tools_cfg: dict | None = None) -> list[ToolSpec]:
    """The seven spotify_* specs bound to a SpotifyHub (hub/spotify.py).
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
        a = args or {}
        r = hub.play(str(a.get("query") or ""),
                     str(a.get("kind") or "track"),
                     str(a.get("device") or ""),
                     shuffle=bool(a.get("shuffle")))
        if r["kind"] == "liked":
            how = " shuffled" if r.get("shuffle") else ""
            return (f"Playing {r['count']} of your Liked Songs{how} on "
                    f"Spotify device “{r['device']}”.")
        who = f" by {r['artists']}" if r.get("artists") else ""
        if r.get("source") == "library":
            where = f"your playlist “{r['name']}”"
        elif r["kind"] != "track":
            where = f"{r['kind']} “{r['name']}”"
        else:
            where = r["name"] + who
        line = f"Playing {where} on Spotify device “{r['device']}”."
        alts = [x for x in (r.get("alternatives") or []) if x]
        if alts:
            line += (" You also have " + ", ".join(f"“{x}”" for x in alts)
                     + " — say which one you meant if it wasn't this.")
        return line

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

    def playlists(args: dict):
        rows = hub.playlists()
        q = normalise_name(str((args or {}).get("query") or ""))
        if q:
            rows = [r for r in rows if q in normalise_name(r["name"])]
        if not rows:
            base = "You have no Spotify playlists yet."
            return base if not q else (f"No Spotify playlist name matches "
                                       f"“{(args or {}).get('query')}”. " +
                                       base)
        return ("Your Spotify playlists: " + "; ".join(
            f"{r['name']}" + (f" ({r['tracks']} tracks)"
                              if r.get("tracks") is not None else "")
            for r in rows))

    runs = {"spotify_play": play, "spotify_control": control,
            "spotify_now_playing": now_playing, "spotify_devices": devices,
            "spotify_transfer": transfer, "spotify_like_current": like_current,
            "spotify_playlists": playlists}
    specs = []
    for name, risk, params, label in SPECS:
        specs.append(ToolSpec(
            name=name, display=name, description=DESCRIPTIONS[name],
            parameters=params, source="hub", risk=risk,
            available=_available(),
            run=runs[name], label=label))
    del cfg
    return specs
