const vscode = require("vscode");
const http = require("node:http");
const path = require("node:path");
const { randomUUID } = require("node:crypto");

const windowId = randomUUID();
let interval;

function accountId() {
  const configured = vscode.workspace.getConfiguration("codexSupervision").get("accountId", "").trim();
  if (configured) return configured;
  const match = (process.env.CODEX_HOME || "").match(/Compte-([12])(?:[\\/]|$)/i);
  return match ? `compte-${match[1]}` : "";
}

function heartbeat() {
  const account = accountId();
  if (!account) return;
  const folders = vscode.workspace.workspaceFolders || [];
  const workspace = folders[0]?.uri.fsPath || vscode.workspace.workspaceFile?.fsPath || "";
  const label = vscode.workspace.name || (workspace ? path.basename(workspace) : "Fenêtre sans projet");
  const port = vscode.workspace.getConfiguration("codexSupervision").get("port", 8765);
  const body = Buffer.from(JSON.stringify({ account_id: account, window_id: windowId, label, workspace,
    workspace_folders: folders.map((folder) => folder.uri.fsPath) }));
  const request = http.request({
    hostname: "127.0.0.1", port, path: "/api/windows/heartbeat", method: "POST", timeout: 1200,
    headers: { "Content-Type": "application/json", "Content-Length": body.length },
  }, (response) => response.resume());
  request.on("timeout", () => request.destroy());
  request.on("error", () => {});
  request.end(body);
}

function activate(context) {
  heartbeat();
  interval = setInterval(heartbeat, 15000);
  context.subscriptions.push(vscode.workspace.onDidChangeWorkspaceFolders(heartbeat));
  context.subscriptions.push(vscode.workspace.onDidChangeConfiguration((event) => {
    if (event.affectsConfiguration("codexSupervision")) heartbeat();
  }));
  context.subscriptions.push({ dispose: () => clearInterval(interval) });
}

function deactivate() {
  clearInterval(interval);
}

module.exports = { activate, deactivate };
