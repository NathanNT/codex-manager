# Telegram alerts and security

Telegram is the only functional external integration. Discord and WhatsApp appear as future options in the UI but do not send messages.

## Pair a private chat

Create a Telegram bot, start a private conversation with it, then run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/configure_telegram.ps1
```

The script uses an existing `TELEGRAM_API_KEY` user or process environment variable, or prompts for the token without displaying it. It shows a random code valid for three minutes. Send `/pair CODE` in **your private chat** with the bot. A prior `/start` or a group message does not pair the chat. The script stores `TELEGRAM_API_KEY`, `TELEGRAM_CHAT_ID`, and `TELEGRAM_PAIRED_CHAT_ID` in Windows **user environment variables**, then restarts the live server. The token is not written to the repository or `alerts.json`. An active Telegram webhook prevents `getUpdates` pairing; remove the webhook first.

Commands and buttons are served only when both the private chat and sender match the pairing. Someone else who starts the bot does not get dashboard data. Keep the bot token and Windows account private. If the token is compromised, regenerate it in BotFather and pair again.

## Views and notifications

The paired chat gets a command menu: `/status` for working chats and requested actions; `/stats` for quotas and tokens; `/projets` for observed project tokens; `/fenetres` for confirmed windows; `/goals` for their goals; `/alertes` for thresholds; and `/historique` for recent changes. `/menu` or `/start` opens the summary. Older aliases remain available. Views have refresh buttons and each request rechecks the paired sender.

Alerts cover completed responses in chats **without** a goal, approvals, errors, observed goal-state changes, and quota/token threshold crossings. Intermediate responses inside a goal and internal subagent completions do not generate the ready-response alert. A ready-response notification contains the account, conversation title, and confirmed window, but not the response text. The **Show response** button fetches the latest final answer for that conversation after the paired user's click. It cannot reply to Codex or browse other chats. The App Server client used for this button only reads conversation and goal state.

Each account has separate quota and token thresholds, alert categories, and a Telegram quiet period. Ready responses, goals, and thresholds during the quiet period are queued; approvals and errors remain immediate. An obsolete queued threshold warning is dropped. Thresholds are notifications, **not spending limits**. The local `/api/preferences` endpoint backs these settings and restricts browser writes to a local origin.

The first scan establishes a baseline and does not retroactively alert on prior goals or usage. The full collector checks accounts every 30 seconds by default and continues tracking unfinished goals beyond the recent-chat list. The lightweight conversation check runs every five seconds; hooks can report a simple-chat completion sooner. Goal states that appear and disappear entirely between scans can be missed. If an ended goal vanishes before a `complete` state is observed, the alert says “completed or removed” because the two cases cannot be distinguished from the next state alone. Failed sends are retried. Health indicators without secrets are available at `http://127.0.0.1:8765/api/health`.
