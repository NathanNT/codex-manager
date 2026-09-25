const workspaceState = {
  overview: null, agents: [], directory: [], channels: [], selectedChannel: "general",
  selectedAgent: null, messages: [], hasMore: false, loadingOlder: false, requestId: 0,
  fetching: false, graphTopology: null, memberSignature: null, lastFetchedKey: null,
  viewSignature: null, pendingLaunchId: null, diagnosticsAgent: null, edgeFilter: null,
  historySignature: null,
  tasks: [], taskSignature: null, taskFilter: "", selectedTask: null, taskDetailRevision: null,
};

function workspaceSetText(element, value) {
  if (element.textContent !== value) element.textContent = value;
}

function workspaceDirectoryAgent(id) {
  return workspaceState.directory.find((agent) => agent.id === id)
    || workspaceState.agents.find((agent) => agent.id === id);
}

function workspaceAgentName(agent) {
  return agent?.name || "Chat Codex";
}

function workspaceChannelName(channel) {
  if (channel.kind === "general") return "Général";
  if (channel.kind === "shared") return channel.name || "Channel partagé";
  const names = channel.members.map((id) =>
    workspaceAgentName(workspaceDirectoryAgent(id)));
  return names.join(" · ");
}

function workspaceStatus(agent) {
  return ({ working: "Au travail", attention: "Action requise", error: "Erreur",
    idle: "Disponible" })[agent.status] || "Statut inconnu";
}

function workspaceShortId(agent) {
  return (agent.session_id || agent.id).slice(0, 8);
}

function workspaceSignalAge(agent) {
  if (!agent.activity_at) return "Signal non vérifié";
  const minutes = Math.max(0, Math.floor((Date.now() / 1000 - agent.activity_at) / 60));
  return minutes < 1 ? "signal récent" : `signal il y a ${minutes} min`;
}

function workspaceAgentDetail(agent) {
  const project = agent.workspace?.split(/[\\/]/).filter(Boolean).at(-1) || "Projet inconnu";
  return `${project} · Chat ${workspaceShortId(agent)} · ${workspaceSignalAge(agent)}`;
}

function tickWorkspaceAgentAges() {
  for (const agent of workspaceState.agents) {
    const row = [...$("workspace-agent-list").children].find((item) => item.dataset.itemKey === agent.id);
    if (row?.querySelector("small")) workspaceSetText(row.querySelector("small"), workspaceAgentDetail(agent));
  }
}

function selectWorkspaceChannel(channelId, agentId = null, edgeFilter = null) {
  if (!workspaceState.channels.some((channel) => channel.id === channelId)) return;
  const changed = workspaceState.selectedChannel !== channelId ||
    JSON.stringify(workspaceState.edgeFilter) !== JSON.stringify(edgeFilter);
  workspaceState.selectedChannel = channelId;
  workspaceState.selectedAgent = agentId;
  workspaceState.edgeFilter = edgeFilter;
  if (changed) {
    workspaceState.messages = [];
    workspaceState.hasMore = false;
    workspaceState.requestId++;
    workspaceState.lastFetchedKey = null;
  }
  patchWorkspaceSelection();
  const graphTopology = JSON.stringify(workspaceGraphElements());
  const topologyChanged = graphTopology !== workspaceState.graphTopology;
  workspaceState.graphTopology = graphTopology;
  renderWorkspaceNetworkGraph(topologyChanged);
  renderWorkspaceMessages();
  if (changed) refreshWorkspaceConversation();
}

function patchWorkspaceSelection() {
  patchWorkspaceNetworkSelection();
  for (const item of $("workspace-conversation-list").children)
    item.classList.toggle("is-selected", item.dataset.itemKey === workspaceState.selectedChannel);
  for (const item of $("workspace-agent-list").children)
    item.classList.toggle("is-selected", item.dataset.itemKey === workspaceState.selectedAgent);
  renderWorkspaceControls();
  renderWorkspaceOutbox();
  renderWorkspaceDiagnostics();
}

function renderWorkspaceOutbox() {
  const target = $("workspace-outbox");
  const peer = workspaceState.overview?.federation;
  const pending = peer?.role === "client" &&
    workspaceState.selectedChannel === peer.channel_id ? peer.pending_outbox || [] : [];
  target.hidden = !pending.length;
  if (!pending.length) return;
  const signature = JSON.stringify([peer.queue_count, pending]);
  if (target.dataset.signature === signature) return;
  target.dataset.signature = signature;
  target.replaceChildren(node("strong", "", `${peer.queue_count} envoi${peer.queue_count > 1 ? "s" : ""} en attente`));
  for (const item of pending) {
    const row = node("p", "", `${new Date(item.created_at).toLocaleString("fr-FR")} · ` +
      `${item.content.slice(0, 140)}${item.content.length > 140 ? "…" : ""}`);
    if (item.error) row.append(node("small", "", `Erreur : ${item.error}`));
    target.append(row);
  }
}

function workspaceDeliveryLabel(message) {
  if (!message.global_id && !message.recipient_count) return "Historique · transmission non suivie";
  if (!message.recipient_count) return "Publié dans le channel";
  const delivered = message.delivered_count || 0;
  if (delivered === message.recipient_count) return "Transmis au contexte";
  const age = Math.max(0, Math.floor((Date.now() - new Date(message.created_at).getTime()) / 60000));
  const delay = age < 1 ? "à l’instant" : `depuis ${age} min`;
  return delivered ? `Transmis à ${delivered}/${message.recipient_count} agents · ${delay}` :
    `En attente de transmission · ${delay}`;
}

function tickWorkspaceDeliveryLabels() {
  for (const element of document.querySelectorAll("#workspace-message-list .workspace-delivery")) {
    const message = workspaceState.messages.find((item) => String(item.id) === element.dataset.messageId);
    if (message) workspaceSetText(element, workspaceDeliveryLabel(message));
  }
}

function renderWorkspaceDiagnostics(result = null) {
  const section = $("workspace-diagnostics");
  section.hidden = !workspaceState.selectedAgent || !!workspaceState.overview?.demo;
  if (section.hidden) return;
  const agent = workspaceState.agents.find((item) => item.id === workspaceState.selectedAgent);
  const summary = workspaceState.overview?.agent_summaries?.[workspaceState.selectedAgent];
  const historySignature = JSON.stringify([workspaceState.selectedAgent, summary]);
  if (historySignature !== workspaceState.historySignature) {
    workspaceState.historySignature = historySignature;
    loadWorkspaceAgentHistory(workspaceState.selectedAgent);
  }
  workspaceSetText($("workspace-agent-summary"), agent
    ? `${workspaceStatus(agent)} · ${agent.remote ? "Workspace distant" : agent.account_id} · ` +
      `${summary?.sent ?? "?"} envoyés · ${summary?.received ?? "?"} reçus · ` +
      `${summary?.pending ?? "?"} en attente`
    : "Agent indisponible");
  if (workspaceState.diagnosticsAgent !== workspaceState.selectedAgent) {
    workspaceState.diagnosticsAgent = workspaceState.selectedAgent;
    workspaceSetText($("workspace-check-summary"), "Sélectionnez Vérifier pour contrôler GitHub, MCP et hooks.");
    $("workspace-check-results").replaceChildren();
  }
  if (!result) return;
  workspaceSetText($("workspace-check-summary"),
    result.origin === "workspace_distant" && !result.reported_at
      ? `Aucun diagnostic encore rapporté par ${result.origin_workspace || "le pair"}`
      : `${result.origin === "workspace_distant" ? `Rapporté par ${result.origin_workspace || "le pair"}` : "Vérifié localement"} le ` +
        new Date(result.reported_at || result.checked_at).toLocaleString("fr-FR"));
  const target = $("workspace-check-results");
  target.replaceChildren();
  for (const [key, title] of [["github", "GitHub"], ["github_mcp", "GitHub MCP"],
    ["mcp", "Workspace MCP"], ["hooks", "Hooks"],
    ["delivery", "Réception"]]) {
    const check = result[key];
    if (!check) continue;
    const row = node("div", `workspace-check-item is-${check.status}`);
    const label = ({ ok: "OK", incomplete: "Incomplet", error: "Erreur", unknown: "Non vérifié" })[check.status];
    row.append(node("strong", "", `${title} · ${label}`), node("span", "", check.detail));
    target.append(row);
    if (key === "github") for (const [part, partTitle] of [["cli", "CLI"],
      ["repository", "Dépôt API"], ["git", "Accès Git"]]) {
      const subcheck = check.checks?.[part];
      if (!subcheck) continue;
      const sub = node("div", `workspace-check-item is-sub is-${subcheck.status}`);
      const sublabel = ({ ok: "OK", incomplete: "Incomplet", error: "Erreur", unknown: "Non vérifié" })[subcheck.status];
      sub.append(node("strong", "", `${partTitle} · ${sublabel}`), node("span", "", subcheck.detail));
      target.append(sub);
    }
    if (key === "hooks") for (const [part, partTitle] of [["installation", "Installés"],
      ["approval", "Exécution autorisée"], ["execution", "Dernier appel"],
      ["prompt", "Contexte CLI · prompt"], ["post_tool", "Contexte CLI · outil"],
      ["vscode_prompt", "Contexte VS Code · prompt"],
      ["vscode_post_tool", "Contexte VS Code · outil"],
      ["vscode", "Contexte VS Code"]]) {
      const subcheck = check.checks?.[part];
      if (!subcheck) continue;
      const sub = node("div", `workspace-check-item is-sub is-${subcheck.status}`);
      const sublabel = ({ ok: "OK", incomplete: "Incomplet", error: "Erreur", unknown: "Non vérifié" })[subcheck.status];
      sub.append(node("strong", "", `${partTitle} · ${sublabel}`), node("span", "", subcheck.detail));
      target.append(sub);
    }
  }
}

async function loadWorkspaceAgentHistory(agentId) {
  const target = $("workspace-agent-history");
  target.replaceChildren(node("small", "", "Chargement des échanges…"));
  try {
    const response = await fetch(`/api/workspace-mcp/agent?${new URLSearchParams({ agent_id: agentId })}`,
      { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    if (agentId !== workspaceState.selectedAgent) return;
    target.replaceChildren();
    if (!result.messages.length) {
      target.append(node("small", "", "Aucun échange observé."));
      return;
    }
    for (const message of result.messages) {
      const row = node("button", "", `${message.direction === "sent" ? "Envoyé" : "Reçu"} · ` +
        message.content.slice(0, 110));
      row.type = "button";
      row.append(node("small", "", `${message.channel_id} · ${new Date(message.created_at).toLocaleString("fr-FR")}`));
      row.addEventListener("click", () => selectWorkspaceChannel(message.channel_id, agentId));
      target.append(row);
    }
  } catch (error) {
    if (agentId === workspaceState.selectedAgent)
      target.replaceChildren(node("small", "", `Historique indisponible : ${error.message}`));
  }
}

async function checkWorkspaceAgent() {
  const agentId = workspaceState.selectedAgent;
  if (!agentId) return;
  const button = $("workspace-check-button");
  button.disabled = true;
  workspaceSetText($("workspace-check-summary"), "Vérification en cours…");
  try {
    const response = await fetch(`/api/workspace-mcp/diagnostics?${new URLSearchParams({ agent_id: agentId })}`,
      { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    if (agentId === workspaceState.selectedAgent) renderWorkspaceDiagnostics(result);
  } catch (error) {
    if (agentId === workspaceState.selectedAgent)
      workspaceSetText($("workspace-check-summary"), `Vérification impossible : ${error.message}`);
  } finally { button.disabled = false; }
}

function reconcileWorkspaceRows(target, entries, keyOf, signatureOf, create) {
  const old = new Map([...target.children].map((item) => [item.dataset.itemKey, item]));
  let previous = null;
  for (const entry of entries) {
    const key = keyOf(entry), signature = signatureOf(entry);
    let item = old.get(key);
    if (!item || item.dataset.signature !== signature) {
      const replacement = create(entry);
      replacement.dataset.itemKey = key;
      replacement.dataset.signature = signature;
      if (item) item.replaceWith(replacement);
      item = replacement;
    }
    const next = previous ? previous.nextSibling : target.firstChild;
    if (item !== next) target.insertBefore(item, next);
    previous = item;
    old.delete(key);
  }
  for (const item of old.values()) item.remove();
}

function renderWorkspaceIndex() {
  const channels = workspaceState.channels;
  workspaceSetText($("workspace-conversation-count"), String(channels.length));
  reconcileWorkspaceRows($("workspace-conversation-list"), channels, (channel) => channel.id,
    (channel) => JSON.stringify([workspaceChannelName(channel), channel.participants,
      channel.messages, channel.members, channel.active_participants]),
    (channel) => {
      const button = node("button", "workspace-conversation-item");
      button.type = "button";
      const active = channel.members.filter((id) => workspaceState.agents.some((agent) => agent.id === id)).length;
      const archived = channel.kind === "private" && active === 0;
      if (archived) button.classList.add("is-archived");
      button.append(node("strong", "", `${channel.kind === "general" ? "# " : ""}${workspaceChannelName(channel)}`),
        node("small", "", `${archived ? "Historique · " : ""}${channel.participants} participants · ${channel.messages} messages`));
      button.addEventListener("click", () => selectWorkspaceChannel(channel.id));
      return button;
    });
  workspaceSetText($("workspace-agent-count"), String(workspaceState.agents.length));
  reconcileWorkspaceRows($("workspace-agent-list"), workspaceState.agents, (agent) => agent.id,
    (agent) => JSON.stringify([agent.name, agent.status, agent.workspace]),
    (agent) => {
      const item = node("button", "workspace-agent-item");
      item.type = "button";
      const name = node("span", "workspace-agent-item-name");
      name.append(node("i", `workspace-dot is-${agent.status}`), node("strong", "", agent.name));
      item.append(name, node("small", "", workspaceAgentDetail(agent)));
      item.addEventListener("click", () => {
        const current = workspaceState.channels;
        const channel = current.find((entry) => entry.kind === "private" && entry.members.includes(agent.id))
          || current.find((entry) => entry.id === "general");
        selectWorkspaceChannel(channel?.id || "general", agent.id);
      });
      return item;
    });
  patchWorkspaceSelection();
}

function renderWorkspaceMessages(older = false) {
  const channel = workspaceState.channels.find((item) => item.id === workspaceState.selectedChannel);
  const names = new Map(workspaceState.directory.map((agent) => [agent.id, agent.name]));
  workspaceSetText($("workspace-chat-peer"), channel ? workspaceChannelName(channel) : "Sélectionnez un channel");
  workspaceSetText($("workspace-chat-detail"), channel
    ? channel.kind === "general" ? "Partagé par tous les agents actifs" :
      channel.kind === "shared" ? `Partagé avec ${workspaceState.overview?.federation?.peer_id || "un autre workspace"}` :
      `${channel.members.length} participants à cet échange`
    : "Les messages apparaîtront ici.");
  workspaceSetText($("workspace-chat-total"), channel ? `${formatNumber(channel.messages)} messages` : "");
  const edgeFilter = workspaceState.edgeFilter;
  $("workspace-clear-filter").hidden = !edgeFilter;
  if (edgeFilter) workspaceSetText($("workspace-chat-detail"),
    `Échanges entre ${workspaceAgentName(workspaceDirectoryAgent(edgeFilter.sender))} et ` +
    workspaceAgentName(workspaceDirectoryAgent(edgeFilter.recipient)));
  const members = $("workspace-channel-members");
  const memberSignature = JSON.stringify([channel?.id, channel?.members.map((id) => {
    const agent = workspaceDirectoryAgent(id);
    return [id, agent?.name, agent?.status, workspaceState.agents.some((item) => item.id === id)];
  })]);
  if (memberSignature !== workspaceState.memberSignature) {
    workspaceState.memberSignature = memberSignature;
    members.replaceChildren();
    if (channel?.kind === "general" && channel.members.length > 8) {
      members.append(node("span", "workspace-member-summary",
        `Tous les ${channel.members.length} agents actifs participent au channel général.`));
    }
    for (const id of channel?.kind === "general" && channel.members.length > 8
      ? [] : channel?.members || []) {
      const agent = workspaceDirectoryAgent(id);
      const active = workspaceState.agents.some((item) => item.id === id);
      const item = node("span", "workspace-member");
      item.append(node("i", `workspace-dot is-${active ? agent?.status || "idle" : "idle"}`),
        node("span", "", agent ? `${agent.name} · ${workspaceShortId(agent)}` : id));
      members.append(item);
    }
  }
  $("workspace-load-older").hidden = !channel || !workspaceState.hasMore;
  const join = $("workspace-join-button");
  const selected = workspaceState.selectedAgent;
  join.hidden = channel?.kind !== "shared" || !selected || !workspaceState.agents.some((agent) => agent.id === selected && !agent.remote) || channel.members.includes(selected);
  const target = $("workspace-message-list");
  const oldHeight = target.scrollHeight, oldTop = target.scrollTop;
  const nearBottom = target.scrollHeight - target.scrollTop - target.clientHeight < 65;
  const second = channel?.kind === "private" && channel.members.length === 2 ? channel.members[1] : null;
  if (workspaceState.messages.length) {
    reconcileWorkspaceRows(target, workspaceState.messages, (message) => String(message.id),
      (message) => JSON.stringify([message.content, message.created_at, message.sender_agent_id,
        names.get(message.sender_agent_id), second, message.recipient_count, message.delivered_count,
        message.references, message.deliveries]),
      (message) => {
        const row = node("li", `workspace-chat-message${second && message.sender_agent_id === second ? " is-right" : ""}`);
        const meta = node("div", "workspace-message-meta");
        const date = node("time", "", new Date(message.created_at).toLocaleString("fr-FR", {
          day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }));
        date.dateTime = message.created_at;
        meta.append(node("strong", "", names.get(message.sender_agent_id) || message.sender_agent_id), date);
        row.append(meta, node("p", "workspace-message-content", message.content));
        if (message.references?.length)
          row.append(node("small", "workspace-references", `Références : ${message.references.join(" · ")}`));
        const delivery = node("small", "workspace-delivery", workspaceDeliveryLabel(message));
        delivery.dataset.messageId = String(message.id);
        if (message.deliveries?.length) delivery.title = message.deliveries.map((item) =>
          `${names.get(item.agent_id) || item.agent_id} : ${item.delivered_at
            ? `transmis via ${item.method === "hook" ? "hook" : "MCP"} le ${new Date(item.delivered_at).toLocaleString("fr-FR")}${item.proof ? ` · ${item.proof}` : ""}`
            : item.attempted_at
              ? `en attente · sortie du hook préparée le ${new Date(item.attempted_at).toLocaleString("fr-FR")}, réception par Codex non confirmée`
              : "en attente · aucune tentative de transmission"}`).join("\n");
        row.append(delivery);
        return row;
      });
  } else {
    const label = channel ? "Aucun message dans ce channel." : "Aucun channel sélectionné.";
    if (target.children.length !== 1 || !target.firstChild.classList.contains("empty"))
      target.replaceChildren(node("li", "empty", label));
    else workspaceSetText(target.firstChild, label);
  }
  if (older) target.scrollTop = target.scrollHeight - oldHeight + oldTop;
  else if (nearBottom) target.scrollTop = target.scrollHeight;
}

async function refreshWorkspaceConversation(before = false) {
  const channelId = workspaceState.selectedChannel;
  if (!workspaceState.channels.some((channel) => channel.id === channelId) ||
      (before && (workspaceState.loadingOlder || !workspaceState.messages.length))) return;
  if (before) workspaceState.loadingOlder = true;
  const requestId = ++workspaceState.requestId;
  const query = new URLSearchParams({ channel_id: channelId, limit: "50" });
  if (workspaceState.edgeFilter) {
    query.set("sender", workspaceState.edgeFilter.sender);
    query.set("recipient", workspaceState.edgeFilter.recipient);
  }
  if (before) query.set("before_id", String(workspaceState.messages[0].id));
  try {
    const response = await fetch(`/api/workspace-mcp/channel?${query}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const result = await response.json();
    if (requestId !== workspaceState.requestId || channelId !== workspaceState.selectedChannel) return;
    const merged = new Map([...(before ? result.messages : workspaceState.messages),
      ...(before ? workspaceState.messages : result.messages)].map((message) => [message.id, message]));
    workspaceState.messages = [...merged.values()].sort((a, b) => a.id - b.id);
    if (before || workspaceState.messages.length <= 50) workspaceState.hasMore = result.has_more;
    if (!before) {
      const current = workspaceState.channels.find((item) => item.id === channelId);
      workspaceState.lastFetchedKey = `${channelId}:${current?.last_id || 0}:${current?.delivery_revision || 0}:${JSON.stringify(workspaceState.edgeFilter)}`;
    }
    renderWorkspaceMessages(before);
  } catch {
    if (requestId === workspaceState.requestId && !workspaceState.messages.length)
      $("workspace-message-list").replaceChildren(node("li", "empty", "Historique indisponible."));
  } finally {
    if (before) workspaceState.loadingOlder = false;
  }
}

function renderWorkspaceMcp(data) {
  const agents = data.agents || [];
  const directory = data.directory || agents;
  const channels = data.channels || [];
  const signature = JSON.stringify([
    agents.map((agent) => [agent.id, agent.name, agent.status, agent.workspace, agent.launch_id]),
    directory.map((agent) => [agent.id, agent.name, agent.status]),
    channels.map((channel) => [channel.id, channel.kind, channel.members,
      channel.participants, channel.messages, channel.last_id, channel.active_participants,
      channel.delivery_revision]),
    data.connections, data.graph_agents?.map((agent) => agent.id), data.graph_activity,
    data.general_senders,
    data.launches?.map((item) => [item.launch_id, item.session_id, item.state, item.error]),
    data.agent_summaries, data.accounts, data.demo, data.tasks,
    data.federation,
  ]);
  if (signature === workspaceState.viewSignature) {
    workspaceState.agents = agents;
    tickWorkspaceAgentAges();
    return;
  }
  workspaceState.viewSignature = signature;
  workspaceState.overview = data;
  workspaceState.agents = agents;
  workspaceState.directory = directory;
  workspaceState.channels = channels;
  workspaceState.tasks = data.tasks || [];
  if (!workspaceState.channels.some((channel) => channel.id === workspaceState.selectedChannel)) {
    workspaceState.selectedChannel = workspaceState.channels[0]?.id || null;
    workspaceState.messages = [];
    workspaceState.hasMore = false;
    workspaceState.lastFetchedKey = null;
  }
  const peer = data.federation;
  workspaceSetText($("workspace-mode"), data.demo ? "Démonstration · données fictives" :
    peer ? `Données réelles · ${peer.connected ? "pair connecté" : "pair indisponible"}${peer.queue_count ? ` · ${peer.queue_count} en attente` : ""}${peer.error ? ` · ${peer.error}` : ""}${peer.queue_error ? ` · erreur d’envoi : ${peer.queue_error}` : ""}` :
      "Données réelles · local");
  const topology = JSON.stringify(workspaceGraphElements());
  const topologyChanged = topology !== workspaceState.graphTopology;
  workspaceState.graphTopology = topology;
  renderWorkspaceNetworkGraph(topologyChanged);
  renderWorkspaceIndex();
  renderWorkspaceTasks();
  renderWorkspaceOutbox();
  renderWorkspaceMessages();
  renderWorkspaceControls();
  const selected = channels.find((channel) => channel.id === workspaceState.selectedChannel);
  if (selected && !workspaceState.loadingOlder &&
      workspaceState.lastFetchedKey !== `${selected.id}:${selected.last_id || 0}:${selected.delivery_revision || 0}:${JSON.stringify(workspaceState.edgeFilter)}`)
    refreshWorkspaceConversation();
}

async function refreshWorkspaceMcp() {
  if (workspaceState.fetching) return;
  workspaceState.fetching = true;
  try {
    const response = await fetch("/api/workspace-mcp", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderWorkspaceMcp(await response.json());
    refreshWorkspaceGraphTheme();
  } catch {
    workspaceSetText($("workspace-mode"), "Connexion momentanément indisponible");
  } finally {
    workspaceState.fetching = false;
  }
}

function workspaceOptions(select, entries, placeholder = null) {
  const selected = select.value;
  const signature = JSON.stringify([placeholder, entries]);
  if (select.dataset.signature !== signature) {
    select.dataset.signature = signature;
    select.replaceChildren();
    if (placeholder) select.append(new Option(placeholder, ""));
    for (const [value, label] of entries) select.append(new Option(label, value));
    if ([...select.options].some((option) => option.value === selected)) select.value = selected;
  }
}

const workspaceTaskColumns = [
  ["todo", "À faire"], ["working", "En cours"], ["review", "À valider"],
  ["blocked", "Bloqué"], ["done", "Terminé"],
];

function workspaceTaskAgent(id) {
  if (!id) return "Non attribuée";
  const agent = workspaceDirectoryAgent(id);
  return agent ? `${agent.name} · ${workspaceShortId(agent)}` : id;
}

function workspaceTaskChannelOptions() {
  return workspaceState.channels.filter((channel) => channel.kind !== "shared" && (channel.kind === "general" ||
    channel.members.some((id) => workspaceState.agents.some((agent) => agent.id === id)) ||
    workspaceState.tasks.some((task) => task.channel_id === channel.id)))
    .map((channel) => [channel.id, workspaceChannelName(channel)]);
}

function workspaceTaskAssigneeOptions(channelId, current = null) {
  const channel = workspaceState.channels.find((item) => item.id === channelId);
  const agents = workspaceState.agents.filter((agent) => !agent.remote &&
    (channelId === "general" || channel?.members.includes(agent.id)));
  const entries = agents.map((agent) => [agent.id, workspaceTaskAgent(agent.id)]);
  if (current && !entries.some(([id]) => id === current))
    entries.push([current, `${workspaceTaskAgent(current)} · hors ligne`]);
  return entries;
}

function selectWorkspaceView(view) {
  const chat = view === "chat";
  $("workspace-kanban-view").hidden = chat;
  $("workspace-chat-view").hidden = !chat;
  if (chat) requestAnimationFrame(() => {
    workspaceGraph?.resize();
    renderWorkspaceNetworkGraph(true);
  });
}

function renderWorkspaceTasks() {
  const tasks = workspaceState.tasks;
  const demo = !!workspaceState.overview?.demo;
  for (const field of $("workspace-task-form").querySelectorAll("input,select,textarea,button"))
    field.disabled = demo;
  $("workspace-task-detail-assignee").disabled = demo;
  $("workspace-task-detail-status").disabled = demo;
  for (const field of $("workspace-task-note-form").querySelectorAll("textarea,button"))
    field.disabled = demo;
  if (demo) workspaceSetText($("workspace-task-form-status"), "Démonstration · modifications désactivées");
  const channels = workspaceTaskChannelOptions();
  workspaceOptions($("workspace-task-channel"), channels);
  workspaceOptions($("workspace-task-filter-channel"), [["", "Tous les channels"], ...channels]);
  $("workspace-task-filter-channel").value = workspaceState.taskFilter;
  const createChannel = $("workspace-task-channel").value || "general";
  workspaceOptions($("workspace-task-assignee"),
    workspaceTaskAssigneeOptions(createChannel), "Non attribuée");
  const filtered = tasks.filter((task) => !workspaceState.taskFilter ||
    task.channel_id === workspaceState.taskFilter);
  const counts = Object.fromEntries(workspaceTaskColumns.map(([status]) =>
    [status, filtered.filter((task) => task.status === status).length]));
  workspaceSetText($("workspace-task-summary"),
    `${filtered.length} tâches · ${counts.working} en cours · ${counts.blocked} bloquées · ${counts.review} à valider`);
  const signature = JSON.stringify([filtered, workspaceState.taskFilter,
    workspaceState.agents.map((agent) => [agent.id, agent.name, agent.status])]);
  if (signature !== workspaceState.taskSignature) {
    workspaceState.taskSignature = signature;
    const board = $("workspace-kanban-board");
    board.replaceChildren();
    for (const [status, label] of workspaceTaskColumns) {
      const column = node("section", "workspace-kanban-column");
      column.dataset.status = status;
      const heading = node("div", "workspace-kanban-heading");
      heading.append(node("strong", "", label), node("span", "", String(counts[status])));
      const list = node("div", "workspace-kanban-list");
      for (const task of filtered.filter((item) => item.status === status)) {
        const card = node("article", `workspace-task-card is-${status}`);
        card.draggable = !demo;
        const title = node("button", "", task.title);
        title.type = "button";
        title.addEventListener("click", () => openWorkspaceTask(task.id));
        card.append(title);
        const assignee = node("p", "workspace-task-assignee");
        const liveAgent = workspaceState.agents.find((agent) => agent.id === task.assignee_agent_id);
        assignee.append(node("i", `workspace-dot is-${liveAgent?.status || "idle"}`),
          node("span", "", `${workspaceTaskAgent(task.assignee_agent_id)}${task.assignee_agent_id ?
            ` · ${liveAgent ? workspaceStatus(liveAgent) : "Hors ligne"}` : ""}`));
        if (task.assignee_agent_id && !liveAgent) assignee.classList.add("is-unavailable");
        card.append(assignee);
        if (task.latest_note) card.append(node("p", "", task.latest_note.slice(0, 150)));
        const channel = workspaceState.channels.find((item) => item.id === task.channel_id);
        card.append(node("small", "", `${channel ? workspaceChannelName(channel) : task.channel_id} · ` +
          new Date(task.updated_at).toLocaleString("fr-FR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })));
        card.addEventListener("dragstart", (event) => {
          event.dataTransfer.effectAllowed = "move";
          event.dataTransfer.setData("text/plain", task.id);
        });
        list.append(card);
      }
      if (!list.childElementCount) list.append(node("p", "workspace-kanban-empty", "Aucune tâche"));
      column.append(heading, list);
      column.addEventListener("dragover", (event) => {
        if (demo) return;
        event.preventDefault();
        column.classList.add("is-drop-target");
      });
      column.addEventListener("dragleave", (event) => {
        if (!column.contains(event.relatedTarget)) column.classList.remove("is-drop-target");
      });
      column.addEventListener("drop", async (event) => {
        if (demo) return;
        event.preventDefault();
        column.classList.remove("is-drop-target");
        const taskId = event.dataTransfer.getData("text/plain");
        if (!tasks.some((task) => task.id === taskId && task.status !== status)) return;
        try {
          await workspacePost("/api/workspace-mcp/task", { action: "update", task_id: taskId, status });
          await refreshWorkspaceMcp();
        } catch (error) { workspaceSetText($("workspace-task-form-status"), `Déplacement impossible : ${error.message}`); }
      });
      board.append(column);
    }
  }
  const selected = tasks.find((task) => task.id === workspaceState.selectedTask);
  if (!selected) {
    $("workspace-task-detail").hidden = true;
    workspaceState.selectedTask = null;
    workspaceState.taskDetailRevision = null;
  } else if (workspaceState.taskDetailRevision !== selected.revision) {
    loadWorkspaceTaskDetail(selected.id, selected.revision);
  }
}

async function openWorkspaceTask(taskId) {
  workspaceState.selectedTask = taskId;
  workspaceState.taskDetailRevision = null;
  $("workspace-task-detail").hidden = false;
  await loadWorkspaceTaskDetail(taskId);
  $("workspace-task-detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function loadWorkspaceTaskDetail(taskId, revision = null) {
  try {
    const response = await fetch(`/api/workspace-mcp/task?${new URLSearchParams({ task_id: taskId })}`,
      { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    if (workspaceState.selectedTask !== taskId) return;
    const task = result.task;
    workspaceState.taskDetailRevision = revision || task.events?.[0]?.id || 0;
    $("workspace-task-detail").hidden = false;
    workspaceSetText($("workspace-task-detail-id"),
      `${workspaceChannelName(workspaceState.channels.find((item) => item.id === task.channel_id) ||
        { kind: "shared", name: task.channel_id })} · ${task.id.slice(0, 13)}`);
    workspaceSetText($("workspace-task-detail-title"), task.title);
    workspaceSetText($("workspace-task-detail-description"), task.description || "Aucune description ajoutée.");
    workspaceOptions($("workspace-task-detail-assignee"),
      workspaceTaskAssigneeOptions(task.channel_id, task.assignee_agent_id), "Non attribuée");
    $("workspace-task-detail-assignee").value = task.assignee_agent_id || "";
    workspaceOptions($("workspace-task-detail-status"), workspaceTaskColumns);
    $("workspace-task-detail-status").value = task.status;
    const events = $("workspace-task-events");
    events.replaceChildren();
    for (const entry of task.events) {
      const row = node("div", "workspace-task-event");
      row.append(node("time", "", new Date(entry.created_at).toLocaleString("fr-FR")),
        node("strong", "", entry.actor_agent_id ? workspaceTaskAgent(entry.actor_agent_id) : "Depuis le Kanban"),
        node("span", "", entry.kind === "created" ? "Tâche créée" :
          entry.kind === "assigned" ? `Attribuée à ${entry.note ? workspaceTaskAgent(entry.note) : "personne"}` :
          `${workspaceTaskColumns.find(([status]) => status === entry.status)?.[1] || entry.status}` +
            (entry.note ? ` · ${entry.note}` : "")));
      events.append(row);
    }
  } catch (error) {
    if (workspaceState.selectedTask === taskId)
      workspaceSetText($("workspace-task-note-status"), `Détail indisponible : ${error.message}`);
  }
}

function renderWorkspaceControls() {
  const overview = workspaceState.overview;
  if (!overview) return;
  const accounts = overview.accounts || [];
  workspaceOptions($("workspace-spawn-account"), accounts.map((item) => [item.id, item.name]));
  const launches = (overview.launches || []).filter((item) => item.channel_id === workspaceState.selectedChannel);
  const available = launches.filter((item) => item.session_id && ["ready", "working"].includes(item.state));
  workspaceOptions($("workspace-recipient"), available.map((item) => {
    const agent = workspaceState.agents.find((entry) => entry.id === `${item.account_id}:${item.session_id}`);
    return [item.launch_id, agent?.name || item.name];
  }), "Choisir un agent CLI");
  const target = $("workspace-recipient");
  const selected = available.find((item) => `${item.account_id}:${item.session_id}` === workspaceState.selectedAgent);
  if (selected) target.value = selected.launch_id;
  else if (workspaceState.selectedAgent) target.value = "";
  const pending = (overview.launches || []).find((item) => item.launch_id === workspaceState.pendingLaunchId);
  if (pending?.session_id && pending.state === "ready") {
    if (pending.channel_id === workspaceState.selectedChannel) target.value = pending.launch_id;
    workspaceState.pendingLaunchId = null;
  }
  if (pending?.state === "error") workspaceState.pendingLaunchId = null;
  const status = pending || launches.find((item) => item.state === "error") || null;
  workspaceSetText($("workspace-launch-status"), status?.state === "starting" ? "Démarrage de Codex…" :
    status?.state === "working" ? "L’agent travaille…" :
    status?.state === "error" ? `Erreur : ${status.error}` : "");
  $("workspace-dismiss-button").hidden = status?.state !== "error";
  $("workspace-dismiss-button").dataset.launchId = status?.state === "error" ? status.launch_id : "";
  const remoteChannel = overview.federation?.role === "client" &&
    workspaceState.selectedChannel === overview.federation.channel_id;
  $("workspace-spawn-button").disabled = overview.demo || remoteChannel || !workspaceState.selectedChannel || !accounts.length;
  $("workspace-spawn-account").disabled = overview.demo || !accounts.length;
  target.disabled = overview.demo || !available.length;
  $("workspace-input").disabled = overview.demo || !available.length;
  $("workspace-send-button").disabled = overview.demo || !target.value;
  $("workspace-stop-button").disabled = overview.demo || !target.value;
}

async function workspacePost(path, payload) {
  const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload), cache: "no-store" });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}

window.addEventListener("DOMContentLoaded", () => {
  setInterval(tickWorkspaceDeliveryLabels, 30000);
  setInterval(tickWorkspaceAgentAges, 30000);
  $("workspace-task-channel").addEventListener("change", () => {
    workspaceOptions($("workspace-task-assignee"),
      workspaceTaskAssigneeOptions($("workspace-task-channel").value), "Non attribuée");
  });
  $("workspace-task-filter-channel").addEventListener("change", () => {
    workspaceState.taskFilter = $("workspace-task-filter-channel").value;
    workspaceState.taskSignature = null;
    renderWorkspaceTasks();
  });
  $("workspace-task-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("workspace-task-form").querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const result = await workspacePost("/api/workspace-mcp/task", {
        action: "create", title: $("workspace-task-title").value.trim(),
        description: $("workspace-task-description").value.trim(),
        channel_id: $("workspace-task-channel").value,
        assignee_agent_id: $("workspace-task-assignee").value || null,
      });
      $("workspace-task-title").value = "";
      $("workspace-task-description").value = "";
      workspaceSetText($("workspace-task-form-status"), "Tâche créée.");
      await refreshWorkspaceMcp();
      await openWorkspaceTask(result.task.id);
    } catch (error) {
      workspaceSetText($("workspace-task-form-status"), `Création impossible : ${error.message}`);
    } finally { button.disabled = false; }
  });
  $("workspace-task-detail-close").addEventListener("click", () => {
    workspaceState.selectedTask = null;
    workspaceState.taskDetailRevision = null;
    $("workspace-task-detail").hidden = true;
  });
  $("workspace-task-detail-assignee").addEventListener("change", async () => {
    const taskId = workspaceState.selectedTask;
    if (!taskId) return;
    try {
      await workspacePost("/api/workspace-mcp/task", { action: "assign", task_id: taskId,
        assignee_agent_id: $("workspace-task-detail-assignee").value || null });
      await refreshWorkspaceMcp();
    } catch (error) {
      workspaceSetText($("workspace-task-note-status"), `Attribution impossible : ${error.message}`);
    }
  });
  $("workspace-task-detail-status").addEventListener("change", async () => {
    const taskId = workspaceState.selectedTask;
    if (!taskId) return;
    try {
      await workspacePost("/api/workspace-mcp/task", { action: "update", task_id: taskId,
        status: $("workspace-task-detail-status").value });
      await refreshWorkspaceMcp();
    } catch (error) {
      workspaceSetText($("workspace-task-note-status"), `Statut non modifié : ${error.message}`);
    }
  });
  $("workspace-task-note-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const taskId = workspaceState.selectedTask;
    const note = $("workspace-task-note").value.trim();
    if (!taskId || !note) return;
    try {
      await workspacePost("/api/workspace-mcp/task", { action: "update", task_id: taskId, note });
      $("workspace-task-note").value = "";
      workspaceSetText($("workspace-task-note-status"), "Note ajoutée.");
      await refreshWorkspaceMcp();
    } catch (error) {
      workspaceSetText($("workspace-task-note-status"), `Note non ajoutée : ${error.message}`);
    }
  });
  $("workspace-clear-filter").addEventListener("click", () =>
    selectWorkspaceChannel(workspaceState.selectedChannel, workspaceState.selectedAgent));
  $("workspace-check-button").addEventListener("click", checkWorkspaceAgent);
  $("workspace-join-button").addEventListener("click", async () => {
    const button = $("workspace-join-button");
    button.disabled = true;
    try {
      await workspacePost("/api/workspace-mcp/join", {
        channel_id: workspaceState.selectedChannel, agent_id: workspaceState.selectedAgent,
      });
      await refreshWorkspaceMcp();
    } catch (error) {
      workspaceSetText($("workspace-check-summary"), `Ajout impossible : ${error.message}`);
    } finally { button.disabled = false; }
  });
  $("workspace-spawn-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("workspace-spawn-button");
    button.disabled = true;
    workspaceSetText($("workspace-launch-status"), "Démarrage de Codex…");
    try {
      const result = await workspacePost("/api/workspace-mcp/spawn", {
        account_id: $("workspace-spawn-account").value,
        channel_id: workspaceState.selectedChannel,
      });
      workspaceState.pendingLaunchId = result.launch.launch_id;
      await refreshWorkspaceMcp();
    } catch (error) {
      workspaceSetText($("workspace-launch-status"), `Erreur : ${error.message}`);
    } finally {
      button.disabled = false;
    }
  });
  $("workspace-recipient").addEventListener("change", () => {
    const launch = (workspaceState.overview?.launches || []).find((item) =>
      item.launch_id === $("workspace-recipient").value);
    workspaceState.selectedAgent = launch?.session_id ? `${launch.account_id}:${launch.session_id}` : null;
    patchWorkspaceSelection();
  });
  $("workspace-compose").addEventListener("submit", async (event) => {
    event.preventDefault();
    const content = $("workspace-input").value.trim();
    const launchId = $("workspace-recipient").value;
    if (!content || !launchId) return;
    const button = $("workspace-send-button");
    button.disabled = true;
    workspaceSetText($("workspace-compose-status"), "Envoi…");
    try {
      await workspacePost("/api/workspace-mcp/send", {
        launch_id: launchId, channel_id: workspaceState.selectedChannel, content,
      });
      $("workspace-input").value = "";
      workspaceSetText($("workspace-compose-status"), "Message transmis à l’agent.");
      await refreshWorkspaceMcp();
    } catch (error) {
      workspaceSetText($("workspace-compose-status"), `Erreur : ${error.message}`);
    } finally {
      button.disabled = false;
    }
  });
  $("workspace-stop-button").addEventListener("click", async () => {
    const launchId = $("workspace-recipient").value;
    if (!launchId) return;
    try {
      await workspacePost("/api/workspace-mcp/stop", { launch_id: launchId });
      workspaceSetText($("workspace-compose-status"), "Agent arrêté.");
      await refreshWorkspaceMcp();
    } catch (error) {
      workspaceSetText($("workspace-compose-status"), `Erreur : ${error.message}`);
    }
  });
  $("workspace-dismiss-button").addEventListener("click", async () => {
    const launchId = $("workspace-dismiss-button").dataset.launchId;
    if (!launchId) return;
    try {
      await workspacePost("/api/workspace-mcp/stop", { launch_id: launchId });
      await refreshWorkspaceMcp();
    } catch (error) {
      workspaceSetText($("workspace-launch-status"), `Erreur : ${error.message}`);
    }
  });
});
