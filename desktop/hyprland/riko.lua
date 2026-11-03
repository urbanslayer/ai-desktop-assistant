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

-- Global shortcuts
hl.bind("SUPER + ALT + T", hl.dsp.exec_cmd(ctl .. " type"))                         -- type to her
hl.bind("SUPER + ALT + R", hl.dsp.exec_cmd(ctl .. " talk-start"))                   -- hold to talk...
hl.bind("SUPER + ALT + R", hl.dsp.exec_cmd(ctl .. " talk-stop"), { release = true }) -- ...release to send
hl.bind("SUPER + ALT + H", hl.dsp.exec_cmd(ctl .. " toggle"))                       -- show / hide her
