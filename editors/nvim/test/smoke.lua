-- Headless smoke test: nvim --headless -u NONE -l smoke.lua <plugin_dir> <server_cmd> <schema_file> <out_file>
local plugin, server, schema, out = arg[1], arg[2], arg[3], arg[4]
vim.opt.rtp:prepend(plugin)
local results = {}
local function log(k, v) results[k] = v end
local function finish(code)
  local f = io.open(out, "w"); f:write(vim.json.encode(results)); f:close()
  vim.cmd(code == 0 and "qa!" or "cq!")
end

local sm = require("schemamig")
sm.setup({ cmd = { server, "lsp" } })
vim.cmd("edit " .. schema)
vim.bo.filetype = "json"
if not vim.wait(10000, function() return vim.lsp.get_clients({ name = "schemamig" })[1] ~= nil end, 50) then
  log("error", "server did not attach"); return finish(1)
end
log("attached", true)

-- edit the buffer (unsaved): rename DIMENSION's "probe" -> "probe_key"
local lines = vim.api.nvim_buf_get_lines(0, 0, -1, false)
local in_dim = false
for i, l in ipairs(lines) do
  if l:find('"const": "DIMENSION"') then in_dim = true end
  if in_dim and l:find('"probe"') then
    lines[i] = l:gsub('"probe"', '"probe_key"')
    if l:find('"probe"%s*$') or l:find('"probe",?%s*$') then in_dim = false end
  end
end
vim.api.nvim_buf_set_lines(0, 0, -1, false, lines)

local ok = vim.wait(10000, function()
  for _, d in ipairs(vim.diagnostic.get(0)) do
    if d.source == "schemamig" and d.message:find("unrecorded") then return true end
  end
end, 50)
local msgs = {}
for _, d in ipairs(vim.diagnostic.get(0)) do table.insert(msgs, d.message) end
log("diagnostics", msgs)
if not ok then log("error", "no unrecorded-change diagnostic"); return finish(1) end

-- ask for code actions on the edited line
local row
for i, l in ipairs(vim.api.nvim_buf_get_lines(0, 0, -1, false)) do
  if l:find('"probe_key": {') then row = i - 1 end
end
local c = vim.lsp.get_clients({ name = "schemamig" })[1]
local params = { textDocument = { uri = vim.uri_from_bufnr(0) },
                 range = { start = { line = row, character = 10 }, ["end"] = { line = row, character = 10 } },
                 context = { diagnostics = {} } }
local resp = c:request_sync("textDocument/codeAction", params, 10000, 0)
local titles, chosen = {}, nil
for _, a in ipairs(resp and resp.result or {}) do
  table.insert(titles, a.title)
  if a.title:find("record as rename%-field") then chosen = a end
end
log("actions", titles)
if not chosen then log("error", "no record-as-rename action"); return finish(1) end

-- recording against an unsaved buffer is refused
local dirty = c:request_sync("workspace/executeCommand", { command = chosen.command.command,
  arguments = { vim.tbl_extend("force", chosen.command.arguments[1], { reason = "x" }) } }, 10000, 0)
log("dirty_refusal", dirty and dirty.result and dirty.result.error)
vim.cmd("silent write") -- what the plugin's command handler does before recording

-- run it with a reason (what the vim.ui.input prompt would supply)
local args = vim.tbl_extend("force", chosen.command.arguments[1], { reason = "probe holds a mapping key",
                                                                     compatibility = "breaking" })
args.prompt_reason = nil
local r = c:request_sync("workspace/executeCommand", { command = chosen.command.command, arguments = { args } }, 10000, 0)
log("record", r and r.result)
-- diagnostics clear once recorded
vim.wait(5000, function()
  for _, d in ipairs(vim.diagnostic.get(0)) do if d.message:find("unrecorded") then return false end end
  return true
end, 50)
local after = {}
for _, d in ipairs(vim.diagnostic.get(0)) do table.insert(after, d.message) end
log("diagnostics_after", after)
finish(0)
