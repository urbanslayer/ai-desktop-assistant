-- ==================
-- KEYBINDINGS
-- ==================
-- `mod` is defined as a global in hyprland.lua
--
-- NOTE ON exec_raw: a few dispatchers (focusmonitor, movewindow to a monitor,
-- focuswindow, exact resize, layoutmsg) aren't demonstrated in the shipped
-- example config, so their Lua table shape is unverified. For those, this file
-- uses hl.dsp.exec_raw("<dispatcher> <args>"), which runs the classic dispatcher
-- string directly -- the same text the old .conf used after the last comma.
-- If exec_raw turns out to take a different form on your build, those specific
-- lines are the ones to adjust; everything else uses verified dispatchers.

-- === Application Launchers ===
hl.bind(mod .. " + return",        hl.dsp.exec_cmd("kitty"))
hl.bind(mod .. " + SPACE",         hl.dsp.exec_cmd("dms ipc call spotlight toggle"))
hl.bind(mod .. " + V",             hl.dsp.exec_cmd("dms ipc call clipboard toggle"))
hl.bind(mod .. " + M",             hl.dsp.exec_cmd("dms ipc call processlist toggle"))
hl.bind(mod .. " + comma",         hl.dsp.exec_cmd("dms ipc call settings toggle"))
hl.bind(mod .. " + N",             hl.dsp.exec_cmd("dms ipc call notifications toggle"))
hl.bind(mod .. " + SHIFT + N",     hl.dsp.exec_cmd("dms ipc call notepad toggle"))
hl.bind(mod .. " + Y",             hl.dsp.exec_cmd("dms ipc call dankdash wallpaper"))
hl.bind(mod .. " + TAB",           hl.dsp.exec_cmd("dms ipc call hypr toggleOverview"))

-- === Cheat sheet ===
hl.bind(mod .. " + SHIFT + Slash", hl.dsp.exec_cmd("dms ipc call keybinds toggle hyprland"))

-- === Security ===
hl.bind(mod .. " + ALT + L",       hl.dsp.exec_cmd("dms ipc call lock lock"))
hl.bind(mod .. " + SHIFT + E",     hl.dsp.exit())
hl.bind("CTRL + ALT + Delete",     hl.dsp.exec_cmd("dms ipc call processlist toggle"))

-- === Audio Controls ===
hl.bind("XF86AudioRaiseVolume", hl.dsp.exec_cmd("dms ipc call audio increment 3"),
    { repeating = true, locked = true })
hl.bind("XF86AudioLowerVolume", hl.dsp.exec_cmd("dms ipc call audio decrement 3"),
    { repeating = true, locked = true })
hl.bind("XF86AudioMute",        hl.dsp.exec_cmd("dms ipc call audio mute"),
    { locked = true })
hl.bind("XF86AudioMicMute",     hl.dsp.exec_cmd("dms ipc call audio micmute"),
    { locked = true })

-- === Brightness Controls ===
hl.bind("XF86MonBrightnessUp",   hl.dsp.exec_cmd('dms ipc call brightness increment 5 ""'),
    { repeating = true, locked = true })
hl.bind("XF86MonBrightnessDown", hl.dsp.exec_cmd('dms ipc call brightness decrement 5 ""'),
    { repeating = true, locked = true })

-- === Window Management ===
hl.bind(mod .. " + Q",         hl.dsp.window.close())
hl.bind(mod .. " + F",         hl.dsp.exec_raw("fullscreen 1"))  -- maximize (old: fullscreen, 1)
hl.bind(mod .. " + SHIFT + F", hl.dsp.exec_raw("fullscreen 0"))  -- full (old: fullscreen, 0)
hl.bind(mod .. " + SHIFT + T", hl.dsp.window.float({ action = "toggle" }))
hl.bind(mod .. " + W",         hl.dsp.group.toggle())

-- === Focus Navigation === (verified: hl.dsp.focus{direction=} with full words)
hl.bind(mod .. " + left",  hl.dsp.focus({ direction = "left" }))
hl.bind(mod .. " + down",  hl.dsp.focus({ direction = "down" }))
hl.bind(mod .. " + up",    hl.dsp.focus({ direction = "up" }))
hl.bind(mod .. " + right", hl.dsp.focus({ direction = "right" }))
hl.bind(mod .. " + H",     hl.dsp.focus({ direction = "left" }))
hl.bind(mod .. " + J",     hl.dsp.focus({ direction = "down" }))
hl.bind(mod .. " + K",     hl.dsp.focus({ direction = "up" }))
hl.bind(mod .. " + L",     hl.dsp.focus({ direction = "right" }))

-- === Window Movement === (verified: hl.dsp.window.move{direction=})
hl.bind(mod .. " + SHIFT + left",  hl.dsp.window.move({ direction = "left" }))
hl.bind(mod .. " + SHIFT + down",  hl.dsp.window.move({ direction = "down" }))
hl.bind(mod .. " + SHIFT + up",    hl.dsp.window.move({ direction = "up" }))
hl.bind(mod .. " + SHIFT + right", hl.dsp.window.move({ direction = "right" }))
hl.bind(mod .. " + SHIFT + H",     hl.dsp.window.move({ direction = "left" }))
hl.bind(mod .. " + SHIFT + J",     hl.dsp.window.move({ direction = "down" }))
hl.bind(mod .. " + SHIFT + K",     hl.dsp.window.move({ direction = "up" }))
hl.bind(mod .. " + SHIFT + L",     hl.dsp.window.move({ direction = "right" }))

-- === Column Navigation === (focuswindow first/last -- via raw dispatcher)
hl.bind(mod .. " + Home", hl.dsp.exec_raw("focuswindow first"))
hl.bind(mod .. " + End",  hl.dsp.exec_raw("focuswindow last"))

-- === Monitor Navigation === (focusmonitor -- via raw dispatcher)
hl.bind(mod .. " + CTRL + left",  hl.dsp.exec_raw("focusmonitor l"))
hl.bind(mod .. " + CTRL + right", hl.dsp.exec_raw("focusmonitor r"))
hl.bind(mod .. " + CTRL + H",     hl.dsp.exec_raw("focusmonitor l"))
hl.bind(mod .. " + CTRL + J",     hl.dsp.exec_raw("focusmonitor d"))
hl.bind(mod .. " + CTRL + K",     hl.dsp.exec_raw("focusmonitor u"))
hl.bind(mod .. " + CTRL + L",     hl.dsp.exec_raw("focusmonitor r"))

-- === Move to Monitor === (movewindow mon: -- via raw dispatcher)
hl.bind(mod .. " + SHIFT + CTRL + left",  hl.dsp.exec_raw("movewindow mon:l"))
hl.bind(mod .. " + SHIFT + CTRL + down",  hl.dsp.exec_raw("movewindow mon:d"))
hl.bind(mod .. " + SHIFT + CTRL + up",    hl.dsp.exec_raw("movewindow mon:u"))
hl.bind(mod .. " + SHIFT + CTRL + right", hl.dsp.exec_raw("movewindow mon:r"))
hl.bind(mod .. " + SHIFT + CTRL + H",     hl.dsp.exec_raw("movewindow mon:l"))
hl.bind(mod .. " + SHIFT + CTRL + J",     hl.dsp.exec_raw("movewindow mon:d"))
hl.bind(mod .. " + SHIFT + CTRL + K",     hl.dsp.exec_raw("movewindow mon:u"))
hl.bind(mod .. " + SHIFT + CTRL + L",     hl.dsp.exec_raw("movewindow mon:r"))

-- === Workspace Navigation === (verified: focus/move with workspace = "e+1")
hl.bind(mod .. " + Page_Down",      hl.dsp.focus({ workspace = "e+1" }))
hl.bind(mod .. " + Page_Up",        hl.dsp.focus({ workspace = "e-1" }))
hl.bind(mod .. " + U",              hl.dsp.focus({ workspace = "e+1" }))
hl.bind(mod .. " + I",              hl.dsp.focus({ workspace = "e-1" }))
hl.bind(mod .. " + CTRL + down",    hl.dsp.window.move({ workspace = "e+1" }))
hl.bind(mod .. " + CTRL + up",      hl.dsp.window.move({ workspace = "e-1" }))
hl.bind(mod .. " + CTRL + U",       hl.dsp.window.move({ workspace = "e+1" }))
hl.bind(mod .. " + CTRL + I",       hl.dsp.window.move({ workspace = "e-1" }))

hl.bind(mod .. " + Z", hl.dsp.layout("togglesplit"))

-- === Move Workspaces ===
hl.bind(mod .. " + SHIFT + Page_Down", hl.dsp.window.move({ workspace = "e+1" }))
hl.bind(mod .. " + SHIFT + Page_Up",   hl.dsp.window.move({ workspace = "e-1" }))
hl.bind(mod .. " + SHIFT + U",         hl.dsp.window.move({ workspace = "e+1" }))
hl.bind(mod .. " + SHIFT + I",         hl.dsp.window.move({ workspace = "e-1" }))

-- === Mouse Wheel Navigation ===
hl.bind(mod .. " + mouse_down",      hl.dsp.focus({ workspace = "e+1" }))
hl.bind(mod .. " + mouse_up",        hl.dsp.focus({ workspace = "e-1" }))
hl.bind(mod .. " + CTRL + mouse_down", hl.dsp.window.move({ workspace = "e+1" }))
hl.bind(mod .. " + CTRL + mouse_up",   hl.dsp.window.move({ workspace = "e-1" }))

-- === Numbered Workspaces === (verified pattern from example config)
for i = 1, 9 do
    hl.bind(mod .. " + " .. i,          hl.dsp.focus({ workspace = i }))
    hl.bind(mod .. " + SHIFT + " .. i,  hl.dsp.window.move({ workspace = i }))
end


hl.bind(mod .. " + " .. 0,          hl.dsp.focus({ workspace = 10 }))
hl.bind(mod .. " + SHIFT + " .. 0,  hl.dsp.window.move({ workspace = 10 }))


-- === Column Management === (layoutmsg preselect -- via raw dispatcher)
hl.bind(mod .. " + bracketleft",  hl.dsp.exec_raw("layoutmsg preselect l"))
hl.bind(mod .. " + bracketright", hl.dsp.exec_raw("layoutmsg preselect r"))

-- === Sizing & Layout ===
hl.bind(mod .. " + R",       hl.dsp.layout("togglesplit"))  -- verified: dsp.layout(string)
hl.bind(mod .. " + CTRL + F", hl.dsp.exec_raw("resizeactive exact 100% 0"))  -- exact-resize via raw

-- === Move/resize windows with mod + LMB/RMB and dragging ===
-- verified: bindm -> { mouse = true }, drag/resize dispatchers take no args
hl.bind(mod .. " + mouse:272", hl.dsp.window.drag(),   { mouse = true, description = "Move window" })
hl.bind(mod .. " + mouse:273", hl.dsp.window.resize(), { mouse = true, description = "Resize window" })

-- === Keyboard resize by keycode === (verified: resize{x=,y=})
hl.bind(mod .. " + code:20", hl.dsp.exec_raw("resizeactive -100 0"), { description = "Expand window left" })
hl.bind(mod .. " + code:21", hl.dsp.exec_raw("resizeactive 100 0"), { description = "Shrink window left" })

-- === Manual Sizing ===
hl.bind(mod .. " + minus",       hl.dsp.exec_raw("resizeactive -10% 0"), { repeating = true })
hl.bind(mod .. " + equal",       hl.dsp.exec_raw("resizeactive 10% 0"), { repeating = true })
hl.bind(mod .. " + SHIFT + minus", hl.dsp.exec_raw("resizeactive 0 -10%"), { repeating = true })
hl.bind(mod .. " + SHIFT + equal", hl.dsp.exec_raw("resizeactive 0 10%"),  { repeating = true })

-- === Screenshots ===
hl.bind("XF86Launch1",        hl.dsp.exec_cmd("grimblast copy area"))
hl.bind("CTRL + XF86Launch1", hl.dsp.exec_cmd("grimblast copy screen"))
hl.bind("ALT + XF86Launch1",  hl.dsp.exec_cmd("grimblast copy active"))
hl.bind("F12",                hl.dsp.exec_cmd("grimblast copy area"))
-- hl.bind("F12",             hl.dsp.exec_cmd([[grim -g "$(slurp)" - | swappy -f -]]))
hl.bind("CTRL + Print",       hl.dsp.exec_cmd("grimblast copy screen"))
hl.bind("ALT + Print",        hl.dsp.exec_cmd("grimblast copy active"))

-- === System Controls === (verified: hl.dsp.dpms exists)
hl.bind(mod .. " + SHIFT + P", hl.dsp.dpms("off"))

-- === Caelestia Temp === --
-- hl.define_submap("global", function()
--     hl.bind(mod .. " + HOME", hl.dsp.global("caelestia:launcher"), { ignore_mods = true })
-- end)
