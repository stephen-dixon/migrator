// Thin VS Code client for the schemamig language server. All logic is in the server;
// this file only prompts for input the server flags (prompt_reason), saves before
// recording, and shows the open change in the status bar.
const vscode = require("vscode");
const { LanguageClient } = require("vscode-languageclient/node");

let client, statusItem;

async function withReason(args) {
  if (!args.prompt_reason) return args;
  const reason = await vscode.window.showInputBox({ prompt: "Reason for this change" });
  if (reason === undefined) return undefined;
  const compatibility = (await vscode.window.showQuickPick(["unknown", "backward-compatible", "breaking"],
    { placeHolder: "Compatibility claim" })) || "unknown";
  const { prompt_reason, ...rest } = args;
  return { ...rest, reason, compatibility };
}

async function exec(command, args) {
  const res = await client.sendRequest("workspace/executeCommand", { command, arguments: [args] });
  if (res && res.error) vscode.window.showErrorMessage(`schemamig: ${res.error}`);
  return res;
}

function showText(text) {
  vscode.workspace.openTextDocument({ content: text, language: "diff" })
    .then((d) => vscode.window.showTextDocument(d, { preview: true }));
}

function activate(context) {
  const cmd = vscode.workspace.getConfiguration("schemamig").get("serverCommand");
  client = new LanguageClient("schemamig", "schemamig",
    { command: cmd[0], args: cmd.slice(1) },
    {
      documentSelector: [{ scheme: "file", language: "json" }],
      middleware: {
        // code actions carry server commands; intercept the ones that need input
        executeCommand: async (command, args, next) => {
          if (command === "schemamig.record" || command === "schemamig.commit") {
            const a = await withReason(args[0] || {});
            if (!a) return;
            await vscode.workspace.saveAll(false); // server refuses unsaved schema edits
            return next(command, [a]);
          }
          return next(command, args);
        },
      },
    });

  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left);
  context.subscriptions.push(statusItem);
  client.onNotification("schemamig/status", (s) => {
    if (!s || !s.open) return statusItem.hide();
    statusItem.text = `$(edit) ${s.class} ${s.target}: ${s.ready ? "ready" : s.problem || "editing"}`;
    statusItem.command = "schemamig.ui.commit";
    statusItem.show();
  });

  const path = () => vscode.window.activeTextEditor?.document.uri.fsPath;
  const reg = (id, fn) => context.subscriptions.push(vscode.commands.registerCommand(id, fn));
  reg("schemamig.ui.begin", async () => {
    const cls = await vscode.window.showInputBox({ prompt: "Change class (e.g. rename-field)" });
    const target = cls && await vscode.window.showInputBox({ prompt: "Target (def:<name>, $.path or #/pointer)" });
    if (target) await exec("schemamig.begin", { class: cls, target, path: path() });
  });
  reg("schemamig.ui.commit", async () => {
    const a = await withReason({ prompt_reason: true });
    if (!a) return;
    await vscode.workspace.saveAll(false);
    await exec("schemamig.commit", a);
  });
  reg("schemamig.ui.abort", () => exec("schemamig.abort", {}));
  reg("schemamig.ui.list", async () => {
    const r = await exec("schemamig.list", { path: path() });
    showText((r.changes || []).map((e) => `${e.id}  ${e.class}  ${e.target}  ${e.reason || ""}`).join("\n") || "journal is empty");
  });
  reg("schemamig.ui.show", async () => {
    const id = await vscode.window.showInputBox({ prompt: "Change id" });
    if (id) { const r = await exec("schemamig.show", { id, path: path() }); if (r.text) showText(r.text); }
  });

  client.start();
}

function deactivate() { return client && client.stop(); }
module.exports = { activate, deactivate };
