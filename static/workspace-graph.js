/* Interactive graph of observed MCP messages, backed by Cytoscape.js. */
let workspaceGraph = null;
let workspaceGraphTheme = null;

function workspaceGraphStyle() {
  const css = getComputedStyle(document.documentElement);
  const color = (name) => css.getPropertyValue(name).trim();
  return [
    { selector: "node", style: {
      "shape": "round-rectangle", "width": 166, "height": 58,
      "background-color": color("--surface"), "border-width": 1,
      "border-color": color("--line-strong"), "color": color("--ink"),
      "label": "data(label)", "font-family": "Inter, Segoe UI, sans-serif",
      "font-size": 11, "font-weight": 500, "text-wrap": "wrap",
      "text-max-width": 146, "text-halign": "center", "text-valign": "center",
    } },
    { selector: 'node[status = "working"]', style: { "border-color": color("--green"), "border-width": 2 } },
    { selector: 'node[status = "attention"]', style: { "border-color": color("--amber"), "border-width": 2 } },
    { selector: 'node[status = "error"]', style: { "border-color": color("--red"), "border-width": 2 } },
    { selector: "node.is-selected", style: { "border-width": 3 } },
    { selector: "edge", style: {
      "curve-style": "bezier", "width": 1.6, "line-color": color("--muted"),
      "target-arrow-shape": "triangle", "target-arrow-color": color("--muted"),
      "arrow-scale": .65, "label": "data(label)", "font-size": 9,
      "color": color("--muted"), "text-background-color": color("--surface-alt"),
      "text-background-opacity": 1, "text-background-padding": "3px",
    } },
    { selector: "edge.is-selected", style: {
      "width": 2.7, "line-color": color("--ink"), "target-arrow-color": color("--ink"),
    } },
  ];
}

function refreshWorkspaceGraphTheme() {
  if (!workspaceGraph) return;
  const theme = document.documentElement.dataset.theme || "light";
  if (theme === workspaceGraphTheme) return;
  workspaceGraphTheme = theme;
  workspaceGraph.style(workspaceGraphStyle());
}

function workspaceGraphElements() {
  const active = new Set(workspaceState.agents.map((agent) => agent.id));
  const channelId = workspaceState.selectedChannel;
  const observed = new Set((workspaceState.overview?.graph_activity || [])
    .filter((item) => item.channel_id === channelId).map((item) => item.agent_id));
  const graphAgents = (workspaceState.overview?.graph_agents || [])
    .filter((agent) => active.has(agent.id) && observed.has(agent.id));
  const graphIds = new Set(graphAgents.map((agent) => agent.id));
  const nodes = graphAgents.map((agent) => ({ data: {
    id: agent.id, label: `${agent.name.slice(0, 27)}\n${workspaceStatus(agent)}`,
    status: agent.status, agentId: agent.id,
  } }));
  const edges = (workspaceState.overview?.connections || [])
    .filter((edge) => edge.channel_id === channelId && graphIds.has(edge.sender) && graphIds.has(edge.recipient))
    .map((edge) => ({ data: {
      id: `${edge.channel_id}|${edge.sender}|${edge.recipient}`,
      source: edge.sender, target: edge.recipient, channelId: edge.channel_id,
      count: edge.messages, label: `${edge.messages} msg`,
    } }));
  return { nodes, edges };
}

function patchWorkspaceNetworkSelection() {
  if (!workspaceGraph) return;
  workspaceGraph.nodes().forEach((item) =>
    item.toggleClass("is-selected", item.id() === workspaceState.selectedAgent));
  workspaceGraph.edges().forEach((item) =>
    item.toggleClass("is-selected", item.data("channelId") === workspaceState.selectedChannel &&
      (!workspaceState.edgeFilter || (item.data("source") === workspaceState.edgeFilter.sender &&
                                   item.data("target") === workspaceState.edgeFilter.recipient))));
}

function renderWorkspaceNetworkGraph(topologyChanged = false) {
  const { nodes, edges } = workspaceGraphElements();
  workspaceSetText($("workspace-graph-summary"),
    `${nodes.length} agents avec messages · ${edges.length} liens observés`);
  $("workspace-graph-empty").hidden = nodes.length > 0;
  if (!window.cytoscape) {
    workspaceSetText($("workspace-graph-empty"), "Le graphe est momentanément indisponible.");
    $("workspace-graph-empty").hidden = false;
    return;
  }
  if (!workspaceGraph) {
    workspaceGraph = cytoscape({
      container: $("workspace-graph"), elements: [],
      style: workspaceGraphStyle(), layout: { name: "preset" },
      minZoom: .45, maxZoom: 1, wheelSensitivity: .18,
    });
    workspaceGraphTheme = document.documentElement.dataset.theme || "light";
    workspaceGraph.on("tap", "node", (event) => {
      const agentId = event.target.data("agentId");
      const current = workspaceState.channels.find((item) => item.id === workspaceState.selectedChannel);
      if (current?.members.includes(agentId)) {
        selectWorkspaceChannel(current.id, agentId);
        return;
      }
      const channel = workspaceState.channels.find((item) => item.kind === "private" &&
        item.members.includes(agentId) && item.active_participants > 1)
        || workspaceState.channels.find((item) => item.id === "general");
      selectWorkspaceChannel(channel?.id || "general", agentId);
    });
    workspaceGraph.on("tap", "edge", (event) =>
      selectWorkspaceChannel(event.target.data("channelId"), null,
        { sender: event.target.data("source"), recipient: event.target.data("target") }));
  }
  refreshWorkspaceGraphTheme();
  const wanted = new Set([...nodes, ...edges].map((item) => item.data.id));
  workspaceGraph.batch(() => {
    workspaceGraph.elements().forEach((item) => { if (!wanted.has(item.id())) item.remove(); });
    for (const element of [...nodes, ...edges]) {
      const existing = workspaceGraph.getElementById(element.data.id);
      if (existing.length) {
        for (const [key, value] of Object.entries(element.data))
          if (existing.data(key) !== value) existing.data(key, value);
      } else workspaceGraph.add(element);
    }
  });
  if (topologyChanged && nodes.length) {
    if (nodes.length <= 3) {
      workspaceGraph.layout({ name: "grid", rows: 1, fit: true, padding: 70,
        sort: (first, second) => first.id().localeCompare(second.id()) }).run();
    } else {
      workspaceGraph.layout({ name: "cose", animate: false, fit: true, padding: 65,
        nodeRepulsion: () => 180000, idealEdgeLength: () => 185 }).run();
    }
  }
  patchWorkspaceNetworkSelection();
}
