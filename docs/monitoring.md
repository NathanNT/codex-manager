# Monitoring and conversations

## What the dashboard shows

Confirmed VS Code windows and workspaces are grouped by account. A window disappears roughly one minute after its last presence signal. Hook events report started, completed, interrupted, and approval-waiting turns. The Codex App Server supplies recent conversation and goal state where available. Chats without a goal are still shown when working. Quotas are **Codex time-window limits**, not a stored credit balance; the dashboard also shows reset times and observed tokens.

Each account can show its ChatGPT subscription type, but Codex App Server does not expose its renewal or end date. Check the account's ChatGPT billing settings for that date.

Each account has configurable quota and token thresholds for today and the previous seven days. Seven-day forecasts require enough observations; within-window exhaustion estimates require at least two observations spanning ten minutes. These are extrapolations, not billing predictions. Browser alerts need explicit browser permission. Telegram notifications are described in [Telegram](telegram.md).

The component catalog lists local Codex CLI versions, cached plugins and versions, available or disabled skills, and configured MCP servers. A missing version is marked as unpublished. MCP URLs and tokens are not sent to the browser.

## Status accuracy

A task is tied to a window only when its account and workspace identify exactly one confirmed window. Sessions without a confirmed window are excluded from the main dashboard. Chats in a project open in multiple windows are shown as having an uncertain window rather than being assigned arbitrarily. A goal recorded as active is shown as working only when its chat is actually working; otherwise the UI shows waiting or needing resumption. A working signal without confirmation becomes uncertain after three minutes and needs checking after twenty.

The separately queried App Server may mark VS Code conversations as unloaded in its own process. The monitor favors hooks and official state where available and can infer recent activity from conversation IDs and update times in local Codex metadata. It does not read chat content for that inference. Some errors, non-approval response requests, and exact window attribution cannot be observed reliably without owning the chat's App Server connection. The UI labels inferred states.

## Token usage by project

Project usage is a local measure of **new token increases observed after tracking starts**, grouped by project and account for today and the last seven days. An initial scan only establishes a baseline. Historical conversation totals, copied chats, other clients on the same account, and turns that cannot be observed may differ from the official account usage. If a chat changes projects between scans, its new tokens go to “Unassigned project”. Project totals are not official ChatGPT quota subdivisions.

## Conversation management

In **Components and chats**, search and filter by account, project, token range, and date. Select a page to archive or restore up to 100 chats per operation. The duplicate review groups titles with `(1)`, `(2)`, and similar suffixes only when the base title and project match. After preview, it keeps the most recently modified chat, removes its suffix, and archives older matches. Active, pinned, or write-locked chats are skipped. This detects duplicate **titles**, not duplicate content.

Selected chat copying between local accounts is limited to ten conversations per operation. The source remains intact. The target is checked with Codex App Server `thread/resume` and `thread/read`; active, still-writing, or already-present chat IDs are refused. Codex does not document an official cross-account import API. Copying therefore depends on the current local session file format. Recorded conversation text and context are copied; external attachments, project files, authentication, quotas, and separately stored goal state are not. Verify a copied chat in its target profile before continuing work.

## Local data and dependencies

Runtime metrics are stored in the ignored `data/monitor.sqlite`. Account calls use credentials in each existing Codex profile; the dashboard does not save authentication tokens. Task titles and metadata stay on the PC except for titles included in opted-in Telegram alerts. The dashboard is bound to `127.0.0.1`.

The navigation SVGs come from [Lucide](https://lucide.dev/) (license in `static/LUCIDE_LICENSE.txt`). The communication graph uses locally served [Cytoscape.js](https://js.cytoscape.org/) 3.34.3 (MIT license in `static/vendor/CYTOSCAPE_LICENSE.txt`). Integration marks are from [Simple Icons](https://github.com/simple-icons/simple-icons) (CC0). The Codex Manager favicon is original to this project.
