-- ==================
-- MONITOR CONFIG
-- ==================
--hl.monitor({
--    output = "DP-1",
--    mode = "1920x1080@75",
--    position = "0x0",
--    scale = 1,
--})


hl.monitor({
    --output = "DP-2",
    output = "desc:Dell Inc. DELL U2518D 3C4YP99OB39L",
    mode = "2560x1440@60",
    position = "0x0",
    scale = 1,
})

hl.monitor({
    --output = "DP-3",
    output  = "desc:Dell Inc. DELL U2518D 3C4YP82K504L",
    mode = "2560x1440@60",
    position = "2560x0",
    scale = 1,
})


hl.monitor({
    --output = "HDMI-A-1",
    output = "desc:GWD ARZOPA 2022110200001",
    mode = "2560x1440@140",
    position = "5120x0",
    scale = 1.25,
    vrr = 1,
})

local CENTRE = "desc:Dell Inc. DELL U2518D 3C4YP82K504L"
local LEFT = "desc:Dell Inc. DELL U2518D 3C4YP99OB39L"
local BOTTOM = "desc:GWD ARZOPA 2022110200001"

-- hl.monitor({
--     output = "eDP-2",
--     mode = "2560x1600@165",
--     position = "1920x0",
--     scale = 1.25,
--     vrr = 1,
-- })

-- Fallback for any unmatched output
-- hl.monitor({ output = "", mode = "preferred", position = "auto", scale = "auto" })

-- ==================
--  WORKSPACE RULES
-- ==================
for i = 1, 4 do
    hl.workspace_rule({ workspace = tostring(i), monitor = CENTRE })
end

for i = 5, 7 do
    hl.workspace_rule({ workspace = tostring(i), monitor = LEFT })
end

-- The old `dwindle:force_split=1` sub-key maps to layout_opts.
hl.workspace_rule({
    workspace = "8",
    monitor = BOTTOM,
    layout = "dwindle",
    layout_opts = { force_split = 2, preserve_split = true },
})

hl.workspace_rule({ workspace = "9", monitor = BOTTOM })

hl.workspace_rule({ workspace = "10", monitor = BOTTOM })
