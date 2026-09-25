const $ = (id) => document.getElementById(id);
let seenEventId = null;
let displayedPreferences = null;
function node(tag, className = "", value = null) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (value !== null) element.textContent = value;
  return element;
}

function formatNumber(value) {
  return typeof value === "number" ? new Intl.NumberFormat("fr-FR").format(value) : "—";
}

function compactNumber(value) {
  return typeof value === "number"
    ? new Intl.NumberFormat("fr-FR", { notation: "compact", maximumFractionDigits: 1 }).format(value)
    : "—";
}

function elapsed(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${Math.floor(seconds)} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
  return `${Math.floor(seconds / 3600)} h ${Math.floor((seconds % 3600) / 60)} min`;
}

function timeOf(timestamp) {
  return timestamp ? new Date(timestamp * 1000).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }) : "—";
}

function dateTimeOf(timestamp) {
  return timestamp ? new Date(timestamp * 1000).toLocaleString("fr-FR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
}

function localDay() {
  const date = new Date();
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

const statusMeta = {
  active: ["Agent au travail", "working"],
  approval: ["Validation requise", "attention"],
  waiting: ["Réponse attendue", "attention"],
  failed: ["Erreur à vérifier", "error"],
  stalled: ["Activité à vérifier", "attention"],
  unconfirmed: ["Activité non confirmée", "attention"],
  completed: ["Tâche terminée", "done"],
  interrupted: ["Tâche arrêtée", "idle"],
};

function taskStatus(task) {
  const [label, kind] = statusMeta[task.display_status] || ["État inconnu", "idle"];
  const status = node("span", `chat-status status-${kind}`);
  const symbol = node("span", "status-symbol", { error: "×", attention: "!", done: "✓", idle: "■" }[kind] || "");
  symbol.setAttribute("aria-hidden", "true");
  if (kind === "working") symbol.dataset.spinKey = `chat:${task.id}`;
  status.append(symbol, node("span", "status-label", label));
  status.title = `${task.signal || task.source || "Signal inconnu"} · dernier signal ${dateTimeOf(task.signal_at)}`;
  if (task.source === "recent_activity") status.title += " · activité estimée, à confirmer";
  return status;
}

function taskDuration(task) {
  if (["recent_activity", "appserver_status"].includes(task.source)) return "durée inconnue";
  const terminal = ["completed", "failed", "interrupted"].includes(task.display_status);
  const end = terminal ? task.ended_at : Date.now() / 1000;
  return end ? elapsed(end - task.started_at) : "durée inconnue";
}

function accountQuota(metrics) {
  const limits = metrics?.limits?.rateLimits;
  const name = limits?.primary ? "primary" : "secondary";
  const bucket = limits?.[name];
  if (!bucket || typeof bucket.usedPercent !== "number") return null;
  return { name, remaining: Math.max(0, 100 - bucket.usedPercent), reset: bucket.resetsAt,
           minutes: bucket.windowDurationMins };
}

function periodLabel(minutes) {
  if (!Number.isFinite(minutes)) return "période inconnue";
  if (minutes % 1440 === 0) return `${minutes / 1440} j`;
  if (minutes % 60 === 0) return `${minutes / 60} h`;
  return `${minutes} min`;
}

function todayTokens(metrics) {
  const bucket = metrics?.usage?.dailyUsageBuckets?.find((item) => item.startDate === localDay());
  return bucket?.tokens ?? null;
}

function sevenDayTokens(metrics) {
  const buckets = metrics?.usage?.dailyUsageBuckets;
  if (!Array.isArray(buckets)) return null;
  const values = new Map(buckets.filter((item) => item.startDate && typeof item.tokens === "number")
    .map((item) => [item.startDate, item.tokens]));
  const date = new Date();
  date.setHours(12, 0, 0, 0);
  let total = 0;
  let observed = false;
  for (let i = 0; i < 7; i++) {
    const day = new Date(date);
    day.setDate(day.getDate() - i);
    const key = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
    if (values.has(key)) observed = true;
    total += values.get(key) || 0;
  }
  return observed ? total : null;
}

function forecast(metrics) {
  const buckets = metrics?.usage?.dailyUsageBuckets;
  if (!Array.isArray(buckets)) return null;
  const values = new Map(buckets.filter((item) => item.startDate && typeof item.tokens === "number")
    .map((item) => [item.startDate, item.tokens]));
  const oldest = [...values.keys()].sort()[0];
  if (!oldest) return null;
  const date = new Date();
  date.setHours(12, 0, 0, 0);
  let total = 0;
  let days = 0;
  for (let i = 1; i <= 7; i++) {
    const day = new Date(date);
    day.setDate(day.getDate() - i);
    const key = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
    if (key >= oldest) {
      total += values.get(key) || 0;
      days++;
    }
  }
  if (days < 3) return null;
  const average = Math.round(total / days);
  return { average, week: average * 7, recent: total };
}

function confirmedTasks(account) {
  const ids = new Set(account.windows.map((windowInfo) => windowInfo.id));
  return account.tasks.filter((task) => ids.has(task.window_id));
}

function latestTasks(tasks) {
  const bySession = new Map();
  for (const task of tasks) {
    const key = task.session_id || task.id;
    const previous = bySession.get(key);
    if (!previous || task.updated_at > previous.updated_at) bySession.set(key, task);
  }
  return [...bySession.values()];
}

function waitingGoalCount(account) {
  const sessions = new Set(confirmedTasks(account).map((task) => task.session_id));
  const goals = new Set([...(account.metrics?.threads?.data || []), ...(account.metrics?.tracked_goals || [])]
    .filter((thread) => sessions.has(thread.id) && thread.goal?.status === "active")
    .map((thread) => thread.id));
  return [...goals].filter((id) => !latestTasks(confirmedTasks(account))
    .some((task) => task.session_id === id && ["active", "unconfirmed"].includes(task.display_status))).length;
}

function taskPriority(task) {
  const status = task.display_status;
  if (status === "failed" && task.updated_at > Date.now() / 1000 - 86400) return 0;
  return ({ approval: 1, waiting: 1, stalled: 2, unconfirmed: 2, active: 3 })[status] ?? 5;
}

function primaryTask(tasks) {
  return latestTasks(tasks).sort((a, b) => taskPriority(a) - taskPriority(b) || b.updated_at - a.updated_at)[0];
}

function appendOverviewSignal(parent, kind, label, symbolText = "") {
  const signal = node("div", `overview-signal signal-${kind}`);
  const symbol = node("span", "overview-symbol", symbolText);
  symbol.setAttribute("aria-hidden", "true");
  if (kind === "working") symbol.dataset.spinKey = "overview:working";
  signal.append(symbol, node("strong", "", label));
  parent.append(signal);
}

function renderOverview(data) {
  const target = $("overview");
  target.replaceChildren();
  const tasks = data.accounts.flatMap((account) => latestTasks(confirmedTasks(account)));
  const active = tasks.filter((task) => task.display_status === "active").length;
  const action = tasks.filter((task) => ["approval", "waiting", "stalled"].includes(task.display_status)).length;
  const uncertain = tasks.filter((task) => task.display_status === "unconfirmed").length;
  const errors = tasks.filter((task) => task.display_status === "failed" && task.updated_at > data.now - 86400).length;
  const goalsWaiting = data.accounts.reduce((count, account) => count + waitingGoalCount(account), 0);
  target.append(node("span", "overview-heading", "En ce moment"));
  const signals = node("div", "overview-signals");
  if (errors) appendOverviewSignal(signals, "error", `${errors} erreur${errors > 1 ? "s" : ""} à vérifier`, "×");
  if (action) appendOverviewSignal(signals, "attention", `${action} action${action > 1 ? "s" : ""} attendue${action > 1 ? "s" : ""}`, "!");
  if (active) appendOverviewSignal(signals, "working", `${active} agent${active > 1 ? "s" : ""} au travail`);
  if (uncertain) appendOverviewSignal(signals, "attention", `${uncertain} activité${uncertain > 1 ? "s" : ""} non confirmée${uncertain > 1 ? "s" : ""}`, "!");
  if (goalsWaiting) appendOverviewSignal(signals, "goal", `${goalsWaiting} goal${goalsWaiting > 1 ? "s" : ""} en attente`, "↻");
  if (!signals.childElementCount) appendOverviewSignal(signals, "idle", "Aucun agent en cours ni action attendue", "✓");
  target.append(signals);
}

function projectName(path) {
  return (path || "Projet non attribué").replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "Projet non attribué";
}

function projectUsageRow(project, weekTotal) {
  const row = node("div", "project-usage-row");
  const identity = node("div", "project-usage-identity");
  identity.append(node("strong", "", projectName(project.workspace)),
                  node("small", "", `${project.chats} chat${project.chats > 1 ? "s" : ""} suivi${project.chats > 1 ? "s" : ""}`));
  identity.title = project.workspace;
  const share = weekTotal ? 100 * project.week / weekTotal : 0;
  const track = node("div", "project-usage-track");
  track.setAttribute("aria-label", `${Math.round(share)} % des tokens suivis sur sept jours`);
  const fill = node("span", "project-usage-fill");
  fill.style.width = `${Math.max(0, Math.min(100, share))}%`;
  track.append(fill);
  identity.append(track);
  row.append(identity, node("span", "project-usage-number", compactNumber(project.today)),
             node("span", "project-usage-number", compactNumber(project.week)));
  return row;
}

function renderProjectUsage(data) {
  const target = $("project-usage-accounts");
  target.replaceChildren();
  for (const account of data.accounts) {
    const usage = data.project_usage?.[account.id] || {};
    const panel = node("article", "project-usage-account");
    const heading = node("header", "project-usage-head");
    heading.append(node("h3", "", account.name));
    const totals = node("div", "project-usage-totals");
    for (const [label, value] of [["Aujourd’hui", usage.today], ["7 jours", usage.week]]) {
      const total = node("div", "project-usage-total");
      total.append(node("span", "", label), node("strong", "", usage.started_at ? compactNumber(value) : "—"));
      if (typeof value === "number") total.title = `${formatNumber(value)} tokens observés`;
      totals.append(total);
    }
    heading.append(totals);
    panel.append(heading);
    const observed = usage.started_at ? `Suivi depuis le ${dateTimeOf(usage.started_at)}` : "Premier relevé en attente";
    const stale = usage.last_observed_at && data.now - usage.last_observed_at > 120;
    panel.append(node("p", `project-usage-since${stale ? " project-usage-stale" : ""}`,
      stale ? `${observed} · dernier relevé ${dateTimeOf(usage.last_observed_at)}` : observed));
    const projects = (usage.projects || []).filter((project) => project.today > 0 || project.week > 0);
    if (!projects.length) {
      panel.append(node("p", "project-usage-empty", usage.started_at
        ? "Aucun nouveau token attribué depuis le début du suivi. Les prochains tours apparaîtront ici."
        : "Le suivi débutera au prochain relevé de Codex."));
    } else {
      const columns = node("div", "project-usage-columns");
      columns.append(node("span", "", "Projet"), node("span", "", "Aujourd’hui"), node("span", "", "7 jours"));
      panel.append(columns);
      const list = node("div", "project-usage-list");
      for (const project of projects.slice(0, 6)) list.append(projectUsageRow(project, usage.week));
      panel.append(list);
      if (projects.length > 6) {
        const rest = node("details", "project-usage-more");
        rest.append(node("summary", "", `Voir ${projects.length - 6} autre${projects.length > 7 ? "s" : ""} projet${projects.length > 7 ? "s" : ""}`));
        for (const project of projects.slice(6)) rest.append(projectUsageRow(project, usage.week));
        panel.append(rest);
      }
    }
    target.append(panel);
  }
}

const goalStatuses = {
  active: ["Goal en cours", "open"],
  paused: ["Goal en pause", "attention"],
  blocked: ["Goal bloqué", "error"],
  usageLimited: ["Goal limité par le quota", "attention"],
  budgetLimited: ["Goal limité par le budget", "attention"],
  complete: ["Goal terminé", "done"],
};

function goalState(thread, task) {
  if (!thread || !Object.hasOwn(thread, "goal")) return ["Goal non vérifié", "unknown"];
  if (!thread.goal) return ["Chat simple · sans goal", "none"];
  if (thread.goal.status === "active") {
    if (task?.display_status === "active") return ["Goal en cours", "open"];
    if (task?.display_status === "unconfirmed") return ["Goal ouvert · activité à confirmer", "attention"];
    if (["approval", "waiting"].includes(task?.display_status)) return ["Goal attend votre réponse", "attention"];
    if (task?.display_status === "interrupted") return ["Goal à reprendre", "attention"];
    return ["Goal en attente", "attention"];
  }
  return goalStatuses[thread.goal.status] || ["Goal : état inconnu", "unknown"];
}

function taskActivity(task, account) {
  const activity = node("div", "task-activity");
  const line = node("div", "task-state-line");
  line.append(taskStatus(task));
  const duration = taskDuration(task);
  if (duration !== "durée inconnue") line.append(node("span", "task-duration", duration));
  activity.append(line, node("p", "task-title", task.title));
  const certainty = task.display_status === "unconfirmed" ? "Dernier signal devenu ancien" :
    task.source === "recent_activity" ? "Activité estimée" :
    task.source === "hook" ? "Confirmé par hook" :
    task.source === "appserver" ? "Tour Codex confirmé" :
    task.source === "appserver_status" ? "État Codex relevé" : null;
  if (certainty) activity.append(node("span", "signal-note", `${certainty} · ${timeOf(task.signal_at)}`));
  const thread = [...(account.metrics?.threads?.data || []), ...(account.metrics?.tracked_goals || [])]
    .find((item) => item.id === task.session_id && Object.hasOwn(item, "goal"));
  const [goalLabel, goalKind] = goalState(thread, task);
  const goal = node("div", `goal-state goal-${goalKind}${goalKind === "open" && task.display_status === "active" ? " goal-moving" : ""}`);
  const goalIcon = node("span", "goal-symbol", { error: "×", attention: "!", done: "✓", none: "−", unknown: "?" }[goalKind] || "");
  goalIcon.setAttribute("aria-hidden", "true");
  if (goalKind === "open" && task.display_status === "active") goalIcon.dataset.spinKey = `goal:${task.id}`;
  goal.append(goalIcon, node("span", "", goalLabel));
  if (thread?.goal?.objective) goal.title = thread.goal.objective;
  activity.append(goal);
  if (task.display_status === "failed" && task.error_message) activity.append(node("p", "task-error", task.error_message));
  return activity;
}

function renderWindow(windowInfo, account) {
  const row = node("div", "window-row");
  const project = node("div", "window-project");
  project.append(node("span", "window-name", windowInfo.label));
  if (windowInfo.workspace) project.title = windowInfo.workspace;
  const matching = latestTasks(account.tasks.filter((task) => task.window_id === windowInfo.id));
  const chosen = primaryTask(matching);
  const thread = chosen && [...(account.metrics?.threads?.data || []), ...(account.metrics?.tracked_goals || [])]
    .find((item) => item.id === chosen.session_id && Object.hasOwn(item, "goal"));
  const isOpenGoal = thread?.goal?.status === "active";
  const kind = !chosen ? "idle" : taskPriority(chosen) === 0 ? "error" :
    ["approval", "waiting", "stalled", "unconfirmed"].includes(chosen.display_status) ? "attention" :
    chosen.display_status === "active" ? "working" : isOpenGoal ? "goal" : "idle";
  row.classList.add(`window-row-${kind}`);
  const workingCount = matching.filter((task) => task.display_status === "active").length;
  if (workingCount > 1) project.append(node("span", "window-multiple", `${workingCount} agents dans cette fenêtre`));
  const activeTasks = matching.filter((task) => ["active", "approval", "waiting", "stalled", "unconfirmed"].includes(task.display_status)
    || (task.display_status === "failed" && task.updated_at > Date.now() / 1000 - 86400));
  const visible = activeTasks.length ? activeTasks.sort((a, b) => taskPriority(a) - taskPriority(b) || b.updated_at - a.updated_at) : chosen ? [chosen] : [];
  const activities = node("div", "window-activities");
  if (!visible.length) activities.append(node("p", "window-empty", "Aucun chat attribué avec certitude"));
  else {
    for (const task of visible.slice(0, 5)) activities.append(taskActivity(task, account));
    if (visible.length > 5) activities.append(node("p", "window-extra", `+ ${visible.length - 5} autre(s) chat(s) actif(s)`));
  }
  row.append(project, activities);
  return row;
}

function windowPriority(windowInfo, account) {
  const task = primaryTask(account.tasks.filter((item) => item.window_id === windowInfo.id));
  if (!task) return 10;
  if (taskPriority(task) <= 3) return taskPriority(task);
  const goal = [...(account.metrics?.threads?.data || []), ...(account.metrics?.tracked_goals || [])]
    .find((item) => item.id === task.session_id && item.goal?.status === "active");
  return goal ? 4 : taskPriority(task);
}

function renderAccount(account, now, projectUsage) {
  const metrics = account.metrics;
  const quota = accountQuota(metrics);
  const tokens = todayTokens(metrics);
  const estimate = forecast(metrics);
  const weekTokens = sevenDayTokens(metrics);
  const credits = metrics?.limits?.rateLimits?.credits;
  const settings = displayedPreferences?.accounts?.[account.id];
  const block = node("article", "account-section");
  block.id = account.id;

  const head = node("header", "account-head");
  const identity = node("div", "account-identity");
  identity.append(node("h3", "account-name", account.name),
                  node("p", "account-email", metrics?.identity?.account?.email || "Compte ChatGPT"));
  const plan = metrics?.identity?.account?.planType;
  const planLabel = ({ pro: "Pro", prolite: "Pro Lite", plus: "Plus", free: "Free", go: "Go" })[plan] || plan || "inconnu";
  identity.append(node("p", "account-plan", `Abonnement ${planLabel} · échéance non fournie`));
  head.append(identity, node("span", "window-count", `${account.windows.length} fenêtre${account.windows.length !== 1 ? "s" : ""} ouverte${account.windows.length !== 1 ? "s" : ""}`));
  block.append(head);

  const metricsRow = node("div", "metrics-row");
  const quotaKind = !quota ? "unknown" : quota.remaining <= (settings?.quota_remaining_percent ?? 20) ? "danger" : quota.remaining < 40 ? "warning" : "ok";
  const quotaItem = node("div", `metric metric-quota quota-${quotaKind}`);
  quotaItem.append(node("span", "metric-label", "Quota disponible"),
                   node("strong", "metric-value", quota ? `${Math.round(quota.remaining)} %` : "—"));
  const track = node("div", "quota-track");
  track.setAttribute("role", "progressbar");
  track.setAttribute("aria-label", `Quota disponible de ${account.name}`);
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", "100");
  if (quota) track.setAttribute("aria-valuenow", String(Math.round(quota.remaining)));
  const fill = node("div", "quota-fill");
  fill.style.width = quota ? `${quota.remaining}%` : "0%";
  track.append(fill);
  const resetNote = !quota ? "Limite indisponible" : quota.reset && quota.reset > Date.now() / 1000
    ? `Période ${periodLabel(quota.minutes)} · réinitialisation ${dateTimeOf(quota.reset)}`
    : `Période ${periodLabel(quota.minutes)} · prochain reset inconnu`;
  quotaItem.append(track, node("span", "metric-note", resetNote));

  const tokenItem = node("div", `metric metric-tokens${tokens !== null && tokens >= (settings?.tokens_daily ?? 100000) ? " token-warning" : ""}`);
  const tokenValue = node("strong", "metric-value", compactNumber(tokens));
  if (tokens !== null) tokenValue.title = `${formatNumber(tokens)} tokens aujourd'hui`;
  tokenItem.append(node("span", "metric-label", "Tokens aujourd’hui"), tokenValue,
                   node("span", "metric-note", tokens === null ? "Relevé non publié" : "D’après le relevé Codex"));
  metricsRow.append(quotaItem, tokenItem);
  block.append(metricsRow);

  const outlook = account.quota_outlook?.[quota?.name];
  const previousPrediction = account.quota_projection?.[quota?.name];
  let outlookKind = "unknown";
  let outlookText = "Prévision indisponible";
  if (outlook?.status === "risk" || (!outlook && previousPrediction)) {
    const predicted = outlook?.at || previousPrediction;
    outlookKind = predicted - now <= 3600 ? "danger" : "warning";
    outlookText = `Limite estimée dans ${elapsed(predicted - now)} · ${dateTimeOf(predicted)}`;
  } else if (outlook?.status === "safe") {
    outlookKind = "safe";
    outlookText = `Quota estimé suffisant jusqu’au reset · ${dateTimeOf(outlook.reset_at)}`;
  } else if (outlook?.status === "exhausted") {
    outlookKind = "danger";
    outlookText = "Quota épuisé · réinitialisation à venir";
  } else if (outlook?.status === "insufficient") {
    outlookText = "Relevés insuffisants pour estimer le rythme";
  }
  const outlookRow = node("div", `quota-outlook outlook-${outlookKind}`);
  outlookRow.append(node("span", "outlook-label", "Prévision"), node("strong", "outlook-value", outlookText));
  block.append(outlookRow);

  const details = node("div", "usage-details");
  const estimateLine = estimate
    ? `Moyenne ${compactNumber(estimate.average)}/j · projection ${compactNumber(estimate.week)}/7j`
    : "Projection indisponible : historique insuffisant";
  const estimateNode = node("span", "", estimateLine);
  if (estimate) estimateNode.title = `7 jours ${formatNumber(estimate.recent)} tokens · moyenne ${formatNumber(estimate.average)}/jour · projection ${formatNumber(estimate.week)} tokens/7 jours`;
  if (weekTokens !== null) details.append(node("span", weekTokens >= (settings?.tokens_weekly ?? 500000) ? "week-warning" : "",
    `7 j glissants : ${compactNumber(weekTokens)} tokens`));
  details.append(estimateNode);
  const secondary = metrics?.limits?.rateLimits?.secondary;
  if (typeof secondary?.usedPercent === "number") details.append(node("span", "", `Autre quota : ${Math.round(Math.max(0, 100 - secondary.usedPercent))} % disponibles · ${periodLabel(secondary.windowDurationMins)}`));
  if (credits?.hasCredits && credits.balance != null) details.append(node("span", "", `Crédits additionnels : ${credits.balance}`));
  else if (credits?.hasCredits === false) details.append(node("span", "", "Crédits additionnels : aucun"));
  const leadingProject = [...(projectUsage?.projects || [])].sort((a, b) => b.today - a.today)[0];
  const projectLink = node("a", "project-leader-link", leadingProject?.today
    ? `Projet principal aujourd’hui : ${projectName(leadingProject.workspace)} · ${compactNumber(leadingProject.today)} tokens`
    : "Consommation par projet · suivi en cours");
  projectLink.href = "#project-usage";
  projectLink.title = leadingProject?.today ? leadingProject.workspace : "Voir la répartition par projet";
  details.append(projectLink);

  block.append(details);

  const activity = node("div", "account-activity");
  const windowsHead = node("div", "list-heading");
  windowsHead.append(node("h4", "", "Activité par fenêtre"));
  activity.append(windowsHead);
  const windows = node("div", "window-list");
  if (!account.windows.length) windows.append(node("p", "empty", "Aucune fenêtre détectée. Activez l'extension de présence dans cet environnement."));
  else for (const windowInfo of [...account.windows].sort((a, b) => windowPriority(a, account) - windowPriority(b, account)))
    windows.append(renderWindow(windowInfo, account));
  activity.append(windows);

  const unassigned = latestTasks(account.tasks.filter((task) => task.window_match === "ambiguous" &&
    ["active", "approval", "waiting", "stalled", "unconfirmed"].includes(task.display_status)));
  if (unassigned.length) {
    const ambiguous = node("div", "unassigned-chats");
    ambiguous.append(node("strong", "", `${unassigned.length} chat${unassigned.length > 1 ? "s" : ""} · fenêtre incertaine`),
                     node("p", "", "Même projet ouvert dans plusieurs fenêtres. Ces chats appartiennent à ce compte, mais leur fenêtre exacte n'est pas identifiable."));
    for (const task of unassigned.slice(0, 4)) ambiguous.append(taskActivity(task, account));
    activity.append(ambiguous);
  }

  const diagnostic = account.diagnostics || {};
  const diag = node("div", `diagnostic${diagnostic.collector_stale ? " diagnostic-stale" : ""}`);
  diag.append(node("span", "", diagnostic.collector_stale ? "⚠ Relevé du compte ancien" : `Relevé ${timeOf(diagnostic.collector_at)}`),
              node("span", "", `Fenêtre ${timeOf(diagnostic.window_at)}`),
              node("span", "", `Dernier hook ${timeOf(diagnostic.hook_at)}`));
  activity.append(diag);

  if (metrics?.collector_error) activity.append(node("p", "collector-error", `Collecte du compte : ${metrics.collector_error}`));
  if (metrics?.hooks?.untrusted) activity.append(node("p", "collector-error", `${metrics.hooks.untrusted} hook(s) à approuver dans le CLI Codex avec /hooks.`));
  block.append(activity);
  return block;
}

function renderSidebar(data) {
  const target = $("sidebar-accounts");
  const selected = window.location.hash;
  target.replaceChildren();
  for (const account of data.accounts) {
    const tasks = latestTasks(confirmedTasks(account));
    const kind = tasks.some((task) => task.display_status === "failed" && task.updated_at > data.now - 86400) ? "error" :
      tasks.some((task) => ["approval", "waiting", "stalled", "unconfirmed"].includes(task.display_status)) ? "attention" :
      tasks.some((task) => task.display_status === "active") ? "working" : "idle";
    const link = node("a", `sidebar-link sidebar-account-link${selected === `#${account.id}` ? " is-current" : ""}`);
    link.href = `#${account.id}`;
    if (selected === link.getAttribute("href")) link.setAttribute("aria-current", "location");
    link.append(node("span", `sidebar-account-dot dot-${kind}`), node("span", "", account.name));
    target.append(link);
  }
}

function settingField(label, name, value, min, max) {
  const wrapper = node("label", "setting-field");
  wrapper.append(node("span", "", label));
  const input = node("input");
  input.type = "number";
  input.name = name;
  input.min = String(min);
  input.max = String(max);
  input.step = "1";
  input.required = true;
  input.value = String(value);
  wrapper.append(input);
  return wrapper;
}

function settingToggle(label, name, checked) {
  const wrapper = node("label", "setting-toggle");
  const input = node("input");
  input.type = "checkbox";
  input.name = name;
  input.checked = checked;
  wrapper.append(input, node("span", "", label));
  return wrapper;
}

function renderPreferences(preferences, accounts) {
  if (!preferences || JSON.stringify(preferences) === JSON.stringify(displayedPreferences)) return;
  if (displayedPreferences && $("preferences-form").matches(":focus-within")) return;
  displayedPreferences = preferences;
  const target = $("preferences-accounts");
  target.replaceChildren();
  for (const account of accounts) {
    const config = preferences.accounts?.[account.id];
    if (!config) continue;
    const group = node("fieldset", "preference-account");
    group.dataset.accountId = account.id;
    group.append(node("legend", "", account.name));
    const fields = node("div", "setting-fields");
    fields.append(settingField("Alerte quota restant ≤ %", "quota_remaining_percent", config.quota_remaining_percent, 1, 100),
                  settingField("Tokens aujourd'hui ≥", "tokens_daily", config.tokens_daily, 1, 1000000000),
                  settingField("Tokens sur 7 jours ≥", "tokens_weekly", config.tokens_weekly, 1, 1000000000));
    group.append(fields);
    const toggles = node("div", "setting-toggles");
    toggles.append(settingToggle("Réponses des chats sans goal", "notify_answers", config.notify_answers),
                   settingToggle("Validations et erreurs", "notify_actions", config.notify_actions),
                   settingToggle("Goals", "notify_goals", config.notify_goals),
                   settingToggle("Quotas", "notify_quota", config.notify_quota),
                   settingToggle("Tokens", "notify_tokens", config.notify_tokens));
    group.append(toggles);
    target.append(group);
  }
  $("quiet-enabled").checked = preferences.quiet_hours.enabled;
  $("quiet-start").value = preferences.quiet_hours.start;
  $("quiet-end").value = preferences.quiet_hours.end;
}

function eventLabel(event) {
  let detail = {};
  try { detail = JSON.parse(event.details || "{}"); } catch { /* old event */ }
  const labels = { UserPromptSubmit: "Chat lancé", PermissionRequest: "Validation demandée",
    Stop: "Réponse terminée", completed: "Réponse terminée", failed: "Erreur",
    Interrupt: "Tour interrompu", interrupted: "Tour interrompu",
    goal_change: `Goal ${detail.status || "modifié"}`, metric_alert: detail.label || "Seuil atteint" };
  return { label: labels[event.type] || event.type, detail: detail.label || "" };
}

function renderHistory(data) {
  const target = $("history-list");
  target.replaceChildren();
  const names = Object.fromEntries(data.accounts.map((account) => [account.id, account.name]));
  const dedup = new Set();
  for (const event of data.events || []) {
    if (event.at < data.now - 7 * 86400) continue;
    const info = eventLabel(event);
    const key = `${event.account_id}:${event.task_id}:${info.label}:${Math.floor(event.at / 10)}`;
    if (dedup.has(key)) continue;
    dedup.add(key);
    const row = node("li", "history-row");
    row.append(node("time", "", timeOf(event.at)),
               node("strong", "", info.label),
               node("span", "", names[event.account_id] || event.account_id));
    if (info.detail) row.append(node("span", "history-detail", info.detail));
    target.append(row);
    if (target.childElementCount >= 20) break;
  }
  if (!target.childElementCount) target.append(node("li", "history-empty", "Aucun changement récent"));
}

function applyRoute(scroll = true) {
  const route = location.hash.slice(1) || "overview";
  const workspace = ["workspace-mcp", "workspace-kanban", "workspace-chat"].includes(route);
  const wasWorkspace = !$("workspace-page").hidden;
  $("dashboard-page").hidden = workspace;
  $("workspace-page").hidden = !workspace;
  if (workspace) selectWorkspaceView(route === "workspace-chat" ? "chat" : "kanban");
  if (workspace && !workspaceState.overview) refreshWorkspaceMcp();
  if (workspace && !$("workspace-chat-view").hidden)
    requestAnimationFrame(() => { workspaceGraph?.resize(); renderWorkspaceNetworkGraph(true); });
  if (!workspace && wasWorkspace) refresh();
  $("header-view").textContent = workspace ?
    `Workspace MCP / ${route === "workspace-chat" ? "Chat" : "Kanban"}` : "Dashboard";
  document.title = `${workspace ? route === "workspace-chat" ? "Chat" : "Kanban" : "Dashboard"} · Codex Manager`;
  for (const link of document.querySelectorAll(".app-sidebar .sidebar-link[href^='#']")) {
    const current = link.getAttribute("href") === `#${route}` || (!location.hash && link.getAttribute("href") === "#overview");
    link.classList.toggle("is-current", current);
    if (current) link.setAttribute("aria-current", workspace ? "page" : "location");
    else link.removeAttribute("aria-current");
  }
  if (!scroll) return;
  const target = document.getElementById(route);
  if (!workspace && target?.tagName === "DETAILS") target.open = true;
  requestAnimationFrame(() => {
    if (workspace) window.scrollTo({ top: 0, behavior: "instant" });
    else if (target) target.scrollIntoView({ behavior: "instant", block: "start" });
    else window.scrollTo({ top: 0, behavior: "instant" });
  });
}

function notifyBrowser(data) {
  if (seenEventId !== null && data.events.length && "Notification" in window && Notification.permission === "granted") {
    const fresh = data.events.filter((event) => event.id > seenEventId && ["PermissionRequest", "Stop", "agent-turn-complete", "failed", "metric_alert"].includes(event.type));
    for (const event of fresh.reverse()) {
      const label = event.type === "PermissionRequest" ? "Validation attendue" : event.type === "failed" ? "Erreur" :
        event.type === "metric_alert" ? "Seuil atteint" : "Tâche terminée";
      let detail = "";
      if (event.type === "metric_alert") {
        try { detail = JSON.parse(event.details || "{}").label || ""; } catch { /* old event */ }
      }
      new Notification(`Codex · ${label}`, { body: `${event.account_id}${detail ? ` · ${detail}` : ""}`, tag: `codex-${event.id}` });
    }
  }
  if (data.events.length) seenEventId = Math.max(seenEventId || 0, data.events[0].id);
}

function render(data) {
  notifyBrowser(data);
  renderPreferences(data.preferences, data.accounts);
  const phases = new Map();
  for (const element of document.querySelectorAll("[data-spin-key]")) {
    const animation = element.getAnimations()[0];
    if (animation && animation.currentTime !== null) phases.set(element.dataset.spinKey, animation.currentTime);
  }
  const phaseTime = performance.now();
  renderOverview(data);
  renderSidebar(data);
  $("accounts").replaceChildren(...data.accounts.map((account) =>
    renderAccount(account, data.now, data.project_usage?.[account.id])));
  renderProjectUsage(data);
  renderHistory(data);
  for (const element of document.querySelectorAll("[data-spin-key]")) {
    const phase = phases.get(element.dataset.spinKey);
    const animation = element.getAnimations()[0];
    if (animation && phase !== undefined) animation.currentTime = phase + performance.now() - phaseTime;
  }
  $("last-refresh").textContent = `Actualisé à ${timeOf(data.now)}`;
  $("footer-time").textContent = new Date().toLocaleDateString("fr-FR", { day: "numeric", month: "long", year: "numeric" });
}

async function refresh() {
  try {
    const response = await fetch("/api/snapshot", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const snapshot = await response.json();
    if ($("workspace-page").hidden) render(snapshot);
    else {
      notifyBrowser(snapshot);
      if (!$("sidebar-accounts").childElementCount) renderSidebar(snapshot);
    }
    workspaceSetText($("connection"), "En direct");
    if (!$("connection").classList.contains("online")) $("connection").classList.add("online");
  } catch {
    workspaceSetText($("connection"), "Hors connexion");
    if ($("connection").classList.contains("online")) $("connection").classList.remove("online");
  }
}

async function init() {
  const themeButton = $("theme-toggle");
  let storedTheme = null;
  try { storedTheme = localStorage.getItem("codex-supervision-theme"); } catch { /* private browsing */ }
  const preferred = window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  document.documentElement.dataset.theme = storedTheme === "dark" || storedTheme === "light" ? storedTheme : preferred;
  const updateThemeButton = () => {
    const dark = document.documentElement.dataset.theme === "dark";
    themeButton.textContent = dark ? "Mode clair" : "Mode sombre";
    themeButton.setAttribute("aria-label", dark ? "Activer le thème clair" : "Activer le thème sombre");
    document.querySelector('meta[name="theme-color"]').content = dark ? "#1f1f1f" : "#f8f8f8";
  };
  updateThemeButton();
  themeButton.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("codex-supervision-theme", next); } catch { /* private browsing */ }
    updateThemeButton();
  });
  document.querySelector(".app-sidebar").addEventListener("click", (event) => {
    const link = event.target.closest("a[href^='#']");
    if (!link) return;
    event.preventDefault();
    if (location.hash !== link.getAttribute("href")) location.hash = link.getAttribute("href");
    applyRoute();
  });
  window.addEventListener("hashchange", () => applyRoute());
  window.addEventListener("resize", () => {
    if (workspaceState.overview) renderWorkspaceGraph();
  });
  $("workspace-load-older").addEventListener("click", () => refreshWorkspaceConversation(true));
  applyRoute(false);
  $("preferences-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    if (!form.reportValidity()) return;
    const result = JSON.parse(JSON.stringify(displayedPreferences));
    for (const group of form.querySelectorAll("[data-account-id]")) {
      const config = result.accounts[group.dataset.accountId];
      for (const key of ["quota_remaining_percent", "tokens_daily", "tokens_weekly"])
        config[key] = Number(group.elements.namedItem(key).value);
      for (const key of ["notify_answers", "notify_actions", "notify_goals", "notify_quota", "notify_tokens"])
        config[key] = group.elements.namedItem(key).checked;
    }
    result.quiet_hours = { enabled: $("quiet-enabled").checked,
      start: $("quiet-start").value, end: $("quiet-end").value };
    const message = $("settings-message");
    message.textContent = "Enregistrement…";
    try {
      const response = await fetch("/api/preferences", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(result) });
      if (!response.ok) throw new Error((await response.json()).error || `HTTP ${response.status}`);
      displayedPreferences = result;
      message.textContent = "Réglages enregistrés";
      await refresh();
    } catch (error) { message.textContent = `Erreur : ${error.message}`; }
  });
  const button = $("notifications");
  if (!("Notification" in window)) button.hidden = true;
  else {
    if (Notification.permission === "granted") button.textContent = "Alertes actives";
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const permission = await Notification.requestPermission();
        button.textContent = permission === "granted" ? "Alertes actives" : "Alertes indisponibles";
      } finally {
        button.disabled = false;
      }
    });
  }
  try {
    const health = await (await fetch("/api/health")).json();
    $("mode").textContent = health.demo ? "Démonstration · données fictives" : "Données réelles · local";
    const telegram = $("integration-telegram");
    const enabled = Boolean(health.telegram_goal_alerts || health.telegram_answer_alerts || health.telegram_action_alerts || health.telegram_metric_alerts);
    telegram.classList.toggle("is-inactive", !enabled);
    telegram.querySelector(".integration-state").textContent = enabled ? "Activé" : "Non configuré";
  } catch { /* refresh below reports the connection state */ }
  await refresh();
  applyRoute();
  const stream = new EventSource("/api/stream");
  stream.addEventListener("update", refresh);
  setInterval(refresh, 5000);
  setInterval(() => {
    if (!$("workspace-page").hidden) refreshWorkspaceMcp();
  }, 4000);
}

window.addEventListener("DOMContentLoaded", init, { once: true });
