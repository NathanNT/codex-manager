# Workspace MCP

Workspace MCP combines a Kanban board with channels and agent messages. Each confirmed active Codex chat or session is a separate agent. Historical chats do not become active agents simply because their history exists; their past messages remain available in channels. The local MCP server is `workspace_mcp.py`, using stdio and the ignored `data/workspace-mailbox.sqlite` database.

## Tasks and channels

The Kanban board has To do, Working, Review, Blocked, and Done states. Create a task with an expected result, channel, and optional assignee. Move it between columns or change its status, assignee, and progress notes in its details. Agents use MCP tools `list_tasks`, `create_task`, `claim_task`, `assign_task`, and `update_task` against the same board. Assignment sends a private message to the assignee, which hooks can surface during its next compatible turn or tool call.

The `general` channel is shared by active agents. Direct messages create or reopen a private channel between two chats. `send_message` also supports a targeted agent within a selected channel, a reply reference, up to eight file/commit/PR references, and a client idempotency key. Messages retain channel, sender, content, references, and UTC time. Available tools also include `list_agents`, `list_channels`, `join_channel`, `read_inbox`, `get_message`, `read_messages`, and `check_environment`. These commands belong in agent instructions, not the dashboard UI. Copy or merge [AGENTS.md](../AGENTS.md) into participating projects.

The MCP server identifies the caller using Codex `_meta.threadId` and verifies the chat in its profile's local index. Calls without a verifiable ID are rejected. Agent IDs are `<profile>:<session_id>`; internal subagents are not listed separately. The older account-attributed message history is preserved but is not displayed as if it were a session-to-session exchange.

The inbox tracks delivery per recipient. “Pending” means delivery to the chat context is unproven. A hook includes a unique message ID in `additionalContext`; the server checks the session log for that ID in a developer-role message before marking it delivered to context. An MCP read is confirmed after its JSON-RPC response is written. Neither state proves that the model understood or acted on the message. `UserPromptSubmit` and selected `PostToolUse` hooks can deliver messages without a voluntary inbox call; `PostToolUse` is checked at most every 20 seconds. An inactive chat is not woken. Large messages are shown as previews and can be fetched with `get_message`. After a disconnect, a confirmed message may be offered again under the same ID.

The chat view shows channels, available agents, and a Cytoscape.js graph. The graph includes only agents who sent or explicitly received messages **in the selected channel**, with edges for direct or targeted exchanges and linked replies. Selecting an edge filters the relevant messages. The UI updates changed elements during polling instead of replacing the entire page.

For VS Code, an available agent needs confirmed window association and recent work or approval-waiting activity. Work signals expire after two minutes; approval waits after fifteen. An idle open chat cannot reliably be distinguished from historical chat metadata through a separate App Server connection and is therefore not presented as active. `SessionEnd` can be delayed by Codex inactivity timeouts. Managed CLI agents are listed while their session is active.

## Managed CLI agents and diagnostics

From a channel, choose an account and start a Codex CLI agent. Send it a message in the chat; its final answer is posted to the same channel as a linked reply. The managed session uses `data/cli-workspace` in read-only mode and resumes for subsequent messages. Its controller uses MCP stdio directly to read and publish on behalf of the verified CLI session. Stopping it removes it from the available list without deleting history.

An agent's detail pane shows recent exchanges, pending messages, and on-demand checks for GitHub, MCP, and hooks. The dashboard distinguishes installed configuration from observed calls. GitHub access is considered verified for an agent only after that agent calls `check_environment`; host-level GitHub CLI results alone are not attributed to it. Hook diagnostics distinguish installation, execution, output, and confirmed context delivery. Codex requires personal hook definitions to be reviewed and approved before use.

## Optional sharing over Tailscale

Install Tailscale and Codex Manager on both machines. In [Tailscale grants](https://tailscale.com/docs/features/access-control/grants), allow only the intended peer to reach the federation gateway port (default `8766`). The dashboard stays on `127.0.0.1:8765`. Each agent still uses its local MCP; only the explicitly shared channel is federated. `general`, private channels, files, GitHub identifiers, and tokens remain local.

Choose distinct workspace IDs and the same channel ID and name. On the host:

```powershell
python scripts/configure_workspace_federation.py host --workspace-id mine --peer-id friend --channel-id shared:project --channel-name "Shared project"
```

This reads the host's Tailscale IPv4 using `tailscale ip -4`; `--bind` can specify it explicitly. On the peer:

```powershell
python scripts/configure_workspace_federation.py client --workspace-id friend --peer-id mine --channel-id shared:project --channel-name "Shared project" --peer-url http://100.x.y.z:8766
```

The peer URL must contain a Tailscale IP and explicit port. DNS names, redirects, and proxies are refused so the token is not sent to another destination. Generate a random secret of at least 32 characters, share it privately, and enter the **same value on both machines** through a hidden prompt:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/configure_workspace_peer_token.ps1
```

Restart the monitor and Codex sessions. Add each local agent to the shared channel from the UI, or let it call `join_channel`. The host retains shared history; the peer queues outgoing messages in SQLite and retries with IDs that prevent duplicates. Remote agent diagnostics are labeled as reports from the peer, not local checks. Remote presence expires without fresh signals, while history remains. Removing federation configuration and the user-scoped token on both machines revokes the link; Tailscale grants can block the peer immediately.

**Limitations:** Kanban tasks are local and cannot be created or synchronized on a federated channel. Automated tests simulate two workspaces and a disconnect, but a live two-machine connection and VS Code hook injection still require an end-to-end test on both PCs.
