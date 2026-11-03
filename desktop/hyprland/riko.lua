-- Riko on Hyprland (Lua config, 0.55+).
-- Add to hyprland.lua (after `mod` is defined):   dofile(os.getenv("HOME") .. "/Development/riko_project/desktop/hyprland/riko.lua")
-- or copy the pieces into windowrules.lua / keybinds.lua.

local RIKO = os.getenv("HOME") .. "/Development/riko_project"
local ctl = RIKO .. "/bin/riko-ctl"

hl.window_rule({
    name = "riko-avatar",
    match = { class = "^(riko)$" },
    float = true,
    pin = true,             -- on every workspace
    border_size = 0,
    no_shadow = true,
    no_blur = true,
    rounding = 0,
    no_anim = true,
    size = "420 680",
    move = "monitor_w-440 monitor_h-700",  -- bottom-right corner
})

-- Global shortcuts (stay off the Super key — that's yours for the WM. These are suggestions:
-- check `hyprctl binds` for clashes and pick other keys if needed.)
hl.bind("CTRL + ALT + T", hl.dsp.exec_cmd(ctl .. " type"))                         -- type to her
hl.bind("CTRL + ALT + SPACE", hl.dsp.exec_cmd(ctl .. " talk-start"))               -- hold to talk...
hl.bind("CTRL + ALT + SPACE", hl.dsp.exec_cmd(ctl .. " talk-stop"), { release = true }) -- ...release to send
hl.bind("CTRL + ALT + H", hl.dsp.exec_cmd(ctl .. " toggle"))                       -- show / hide her
hl.bind("CTRL + ALT + P", hl.dsp.exec_cmd(ctl .. " persona-next"))                 -- next persona (cycles)
