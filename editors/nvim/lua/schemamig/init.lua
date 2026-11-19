-- schemamig for Neovim (>= 0.10): attaches the schemamig language server to JSON buffers
-- inside a project with schemamig.yaml, prompts for reasons where the server asks, and
-- exposes user commands plus a statusline component.
--
--   require("schemamig").setup({ cmd = { "schemamig", "lsp" } })
--   vim.o.statusline = "%f %{v:lua.require'schemamig'.statusline()}"
--
-- Code actions (`vim.lsp.buf.code_action()`, e.g. on an unrecorded-edit warning) offer
-- "record as <class>", "begin <class>", commit and abort.
local M = { status = nil }

local function client()
  return vim.lsp.get_clients({ name = "schemamig" })[1]
end

local function exec(command, args, cb)
  local c = client()
  if not c then
    vim.notify("schemamig: language server not attached", vim.log.levels.WARN)
    return
  end
  args = vim.tbl_extend("force", { path = vim.api.nvim_buf_get_name(0) }, args or {})
  c:request("workspace/executeCommand", { command = command, arguments = { args } }, function(err, res)
    if err then
      vim.notify("schemamig: " .. err.message, vim.log.levels.ERROR)
    elseif res and res.error then
      vim.notify("schemamig: " .. res.error, vim.log.levels.ERROR)
    elseif cb then
      cb(res)
    end
  end, 0)
end

local function with_reason(args, cb)
  if not args.prompt_reason then
    return cb(args)
  end
  vim.ui.input({ prompt = "Reason for this change: " }, function(reason)
    if reason == nil then
      return
    end
    args.prompt_reason = nil
    args.reason = reason
    vim.ui.select({ "unknown", "backward-compatible", "breaking" }, { prompt = "Compatibility:" }, function(c)
      args.compatibility = c or "unknown"
      cb(args)
    end)
  end)
end

local function scratch(text)
  vim.cmd("botright new")
  local buf = vim.api.nvim_get_current_buf()
  vim.bo[buf].buftype, vim.bo[buf].bufhidden, vim.bo[buf].filetype = "nofile", "wipe", "diff"
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, vim.split(text, "\n"))
end

-- client-side handlers for commands that need input (code actions route through these)
for _, name in ipairs({ "schemamig.record", "schemamig.commit" }) do
  vim.lsp.commands[name] = function(command, _)
    local args = (command.arguments or {})[1] or {}
    with_reason(args, function(a)
      vim.cmd("silent! wall") -- the server refuses to record against unsaved schema edits
      exec(name, a, function(res)
        local r = res.record or {}
        vim.notify(("schemamig: recorded %s %s %s"):format(r.id or "", r.class or "", r.target or ""))
      end)
    end)
  end
end

function M.statusline()
  local s = M.status
  if not s or not s.open then
    return ""
  end
  return ("[schemamig %s %s: %s]"):format(s.class, s.target, s.ready and "ready" or (s.problem or "editing"))
end

function M.setup(opts)
  opts = vim.tbl_extend("force", { cmd = { "schemamig", "lsp" } }, opts or {})
  vim.api.nvim_create_autocmd("FileType", {
    pattern = "json",
    callback = function(ev)
      local root = vim.fs.root(ev.buf, { "schemamig.yaml" })
      if not root then
        return
      end
      vim.lsp.start({
        name = "schemamig",
        cmd = opts.cmd,
        root_dir = root,
        handlers = {
          ["schemamig/status"] = function(_, result)
            M.status = result
            vim.cmd("redrawstatus")
          end,
        },
      }, { bufnr = ev.buf })
    end,
  })

  local cmd = vim.api.nvim_create_user_command
  cmd("SchemamigBegin", function(o)
    local cls, target = o.fargs[1], o.fargs[2]
    exec("schemamig.begin", { class = cls, target = target, reason = table.concat(o.fargs, " ", 3) },
      function(res) M.status = res.status; vim.notify("schemamig: opened " .. cls .. " " .. target) end)
  end, { nargs = "+", desc = "schemamig: begin <class> <target> [reason...]" })
  cmd("SchemamigCommit", function()
    vim.lsp.commands["schemamig.commit"]({ arguments = { { prompt_reason = true } } })
  end, {})
  cmd("SchemamigAbort", function() exec("schemamig.abort", {}, function() vim.notify("schemamig: aborted") end) end, {})
  cmd("SchemamigStatus", function()
    exec("schemamig.status", {}, function(res) scratch(vim.json.encode(res)) end)
  end, {})
  cmd("SchemamigList", function()
    exec("schemamig.list", {}, function(res)
      local lines = {}
      for _, e in ipairs(res.changes or {}) do
        table.insert(lines, ("%s  %-16s %-28s %s"):format(e.id, e.class, e.target, e.reason or ""))
      end
      scratch(#lines > 0 and table.concat(lines, "\n") or "journal is empty")
    end)
  end, {})
  cmd("SchemamigShow", function(o)
    exec("schemamig.show", { id = o.args }, function(res) scratch(res.text) end)
  end, { nargs = 1 })
end

-- exposed for tests
M._exec = exec
return M
