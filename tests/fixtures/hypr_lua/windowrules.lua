-- ==================
-- WINDOW RULES
-- ==================
-- Rules are evaluated top to bottom; the last matching effect wins.

hl.window_rule({
    name = "windowrule-1",
    match = { title = "btop" },
    opacity = "0.9 0.9",
})

hl.window_rule({
    name = "windowrule-2",
    match = { class = [[^(org\.wezfurlong\.wezterm)$]] },
    tile = true,
    border_size = 0,
})

hl.window_rule({
    name = "windowrule-3",
    match = { class = [[^(org\.gnome\.)]] },
    rounding = 12,
    border_size = 0,
})

hl.window_rule({
    name = "windowrule-4",
    match = { class = "^(gnome-control-center)$" },
    tile = true,
})

hl.window_rule({
    name = "windowrule-5",
    match = { class = "^(pavucontrol)$" },
    tile = true,
})

hl.window_rule({
    name = "windowrule-6",
    match = { class = "^(nm-connection-editor)$" },
    tile = true,
})

hl.window_rule({
    name = "windowrule-7",
    match = { class = "^(gnome-calculator)$" },
    float = true,
})

hl.window_rule({
    name = "windowrule-8",
    match = { class = "^(galculator)$" },
    float = true,
})

hl.window_rule({
    name = "windowrule-9",
    match = { class = "^(blueman-manager)$" },
    float = true,
})

hl.window_rule({
    name = "windowrule-10",
    match = { class = [[^(org\.gnome\.Nautilus)$]] },
    float = true,
})

hl.window_rule({
    name = "windowrule-11",
    match = { class = "^(steam)$" },
    float = true,
})

hl.window_rule({
    name = "windowrule-12",
    match = { class = "^(xdg-desktop-portal)$" },
    float = true,
})

hl.window_rule({
    name = "windowrule-13",
    match = { class = "^(Alacritty)$" },
    border_size = 0,
})

hl.window_rule({
    name = "windowrule-14",
    match = { class = "^(zen)$" },
    border_size = 0,
})

hl.window_rule({
    name = "windowrule-15",
    match = { class = [[^(com\.mitchellh\.ghostty)$]] },
    border_size = 0,
})

hl.window_rule({
    name = "windowrule-16",
    match = { class = "^(kitty)$" },
    border_size = 0,
})

hl.window_rule({
    name = "windowrule-17",
    match = {
        class = "^(firefox)$",
        title = "^(Picture-in-Picture)$",
    },
    float = true,
})

hl.window_rule({
    name = "windowrule-18",
    match = { class = "^(zoom)$" },
    float = true,
})

-- Dim unfocused tiled windows
hl.window_rule({
    name = "windowrule-19",
    match = {
        float = false,
        focus = false,
    },
    opacity = "0.9 0.9",
})

hl.window_rule({
    name = "windowrule-20",
    match = {
	title = "HoYoPlay",
	class = "steam_app_default"
    },
    float = true,
})

-- ==================
-- LAYER RULES
-- ==================
hl.layer_rule({
    name = "layerrule-1",
    match = { namespace = "^(quickshell)$" },
    no_anim = true,
})
