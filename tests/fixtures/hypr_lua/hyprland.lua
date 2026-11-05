-- Hyprland Configuration
-- https://wiki.hypr.land/Configuring/

-- ==================
-- MODULES
-- ==================
-- NOTE: these are declared GLOBAL (no `local`) so the required files can see
-- them. `local`s in this file do not propagate into require()d modules.
mod = "SUPER"

require("monitors")
require("windowrules")

-- ==================
-- ENVIRONMENT VARS
-- ==================
hl.env("QT_QPA_PLATFORM", "wayland")
hl.env("ELECTRON_OZONE_PLATFORM_HINT", "auto")
hl.env("QT_QPA_PLATFORMTHEME", "qt6ct")
hl.env("QT_QPA_PLATFORMTHEME_QT6", "qt6ct")
hl.env("TERMINAL", "kitty")

-- ==================
-- STARTUP APPS
-- ==================
hl.on("hyprland.start", function()
    hl.exec_cmd('bash -c "wl-paste --watch cliphist store &"')
    hl.exec_cmd("dms run")
--    hl.exec_cmd("/usr/lib/mate-polkit/polkit-mate-authentication-agent-1")
    hl.exec_cmd("systemctl --user start hyprpolkitagent")
    hl.exec_cmd("/home/user/Scripts/unlock_wallet.sh")
    --hl.exec_cmd("/usr/lib/pam_kwallet_init")
    -- hl.exec_cmd("/home/user/Applications/pcloud")
    -- hl.exec_cmd("/home/user/Applications/pcloud_620b8730985354bbb0a5af4ddacd29db")
    hl.exec_cmd("/home/user/Scripts/pcloud.sh")
    -- hl.exec_cmd spawns async; the trailing `&` from the old config is not needed
    -- hl.exec_cmd("waynergy -c 192.0.2.185 -p 24800 -N Asuka")
    hl.exec_cmd("hypridle")
    hl.exec_cmd("xembedsniproxy")
    hl.exec_cmd("/home/user/Scripts/tresorit.sh")
end)

-- ==================
-- GENERAL / INPUT / DECORATION / MISC
-- ==================
hl.config({
    input = {
        kb_layout = "gb",
        kb_options = "grp:sclk_toggle",
    },

    general = {
        gaps_in = 5,
        gaps_out = 5,
        border_size = 0, -- off in niri

        col = {
            active_border = "rgba(707070ff)",
            inactive_border = "rgba(d0d0d0ff)",
        },

        layout = "dwindle",
    },

    decoration = {
        rounding = 12,

        active_opacity = 1.0,
        inactive_opacity = 0.9,

        shadow = {
            enabled = false,
            range = 30,
            render_power = 5,
            offset = { 0, 5 },
            color = "rgba(00000070)",
        },
    },

    animations = {
        enabled = true,
    },

    dwindle = {
        preserve_split = true,
	force_split = 2,
	split_bias = 0
    },

    master = {
        mfact = 0.5,
    },

    misc = {
        disable_hyprland_logo = true,
        disable_splash_rendering = true,
        vrr = 1,
    },

    xwayland = {
        force_zero_scaling = true,
    },
})

-- Keybinds BEFORE animations, so if the animation block errors it can't
-- take the binds down with it.
require("keybinds")

-- ==================
-- ANIMATIONS
-- ==================
-- Curves must be defined before being referenced. These are Hyprland's
-- built-in defaults (from the shipped example config).
hl.curve("easeOutQuint",   { type = "bezier", points = { {0.23, 1},    {0.32, 1} } })
hl.curve("linear",         { type = "bezier", points = { {0, 0},       {1, 1} } })
hl.curve("almostLinear",   { type = "bezier", points = { {0.5, 0.5},   {0.75, 1} } })
hl.curve("quick",          { type = "bezier", points = { {0.15, 0},    {0.1, 1} } })
hl.curve("easy",           { type = "spring", mass = 1, stiffness = 71.2633, dampening = 15.8273644 })

-- Your original config animated: windowsIn, windowsOut, workspaces,
-- windowsMove, fade, border. Mapped onto real leaf names + curves.
hl.animation({ leaf = "windowsIn",   enabled = true, speed = 4.1,  spring = "easy" })
hl.animation({ leaf = "windowsOut",  enabled = true, speed = 1.49, bezier = "linear" })
hl.animation({ leaf = "windowsMove", enabled = true, speed = 4.79, spring = "easy" })
hl.animation({ leaf = "workspaces",  enabled = true, speed = 1.94, bezier = "almostLinear" })
hl.animation({ leaf = "fade",        enabled = true, speed = 3.03, bezier = "quick" })
hl.animation({ leaf = "border",      enabled = true, speed = 5.39, bezier = "easeOutQuint" })
