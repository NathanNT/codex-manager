(() => {
  const byId = (id) => document.getElementById(id);
  const element = (tag, className = "", content = "") => {
    const item = document.createElement(tag);
    item.className = className;
    item.textContent = content;
    return item;
  };
  const number = (value) => Number.isFinite(value) ? new Intl.NumberFormat("fr-FR").format(value) : "—";
  const basename = (path) => (path || "Sans projet").replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "Sans projet";
  const date = (value) => value ? new Date(value * 1000).toLocaleString("fr-FR", {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
  let accounts = [];
  let catalog = null;
  let chats = null;
  let offset = 0;
  let selected = new Set();
  let searchTimer;
  const archivedView = () => byId("chat-view").value === "archived";

  function summary(title, values, renderer) {
    const details = element("details", "inventory-group");
    details.append(element("summary", "", `${title} · ${values.length}`));
    const list = element("ul", "inventory-list");
    if (!values.length) list.append(element("li", "inventory-empty", "Aucun élément trouvé"));
    for (const value of values) list.append(renderer(value));
    details.append(list);
    return details;
  }

  function itemRow(name, note, secondary = "") {
    const item = element("li", "inventory-item");
    const title = element("span", "inventory-name", name);
    const meta = element("span", "inventory-meta", note);
    item.append(title, meta);
    if (secondary) item.append(element("small", "inventory-secondary", secondary));
    return item;
  }

  function renderInventory() {
    const target = byId("catalog-accounts");
    target.replaceChildren();
    for (const account of catalog.accounts) {
      const panel = element("article", "catalog-account");
      const head = element("div", "catalog-account-head");
      const title = element("div");
      title.append(element("h3", "", account.name),
        element("p", "", account.inventory.codex_version || "Version Codex inconnue"));
      head.append(title, element("strong", "chat-total", `${number(account.chats.total)} chats`));
      panel.append(head);
      const counts = element("div", "catalog-counts");
      counts.append(element("span", "", `${account.inventory.plugins.length} plugins`),
        element("span", "", `${account.inventory.skills.filter((item) => item.enabled).length} skills actifs`),
        element("span", "", `${account.inventory.mcp.length} serveurs MCP`));
      panel.append(counts);
      const workspaces = element("div", "workspace-distribution");
      workspaces.append(element("h4", "", "Répartition par projet"));
      for (const part of account.chats.by_workspace) {
        const line = element("div", "distribution-line");
        line.title = part.workspace;
        line.append(element("span", "", basename(part.workspace)), element("strong", "", number(part.count)));
        workspaces.append(line);
      }
      panel.append(workspaces);
      panel.append(summary("Plugins", account.inventory.plugins,
        (item) => itemRow(item.name, item.version || "Version inconnue", item.source)));
      panel.append(summary("Skills", account.inventory.skills,
        (item) => itemRow(item.name, item.version || "Version non publiée",
          `${item.source} · ${item.enabled ? "Disponible" : "Désactivé"}`)));
      panel.append(summary("Serveurs MCP", account.inventory.mcp,
        (item) => itemRow(item.name, item.version || "Version non publiée",
          `${item.transport} · ${item.enabled ? "Configuré" : "Désactivé"}`)));
      target.append(panel);
    }
  }

  function renderChats() {
    const target = byId("chat-list");
    target.replaceChildren();
    if (!chats?.chats?.length) target.append(element("p", "empty", "Aucune conversation trouvée."));
    for (const chat of chats?.chats || []) {
      const row = element("label", "chat-catalog-row");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = chat.id;
      box.checked = selected.has(chat.id);
      box.disabled = !archivedView() && ["active", "approval", "waiting", "stalled"].includes(chat.activity_status);
      box.addEventListener("change", () => {
        if (box.checked) selected.add(chat.id);
        else selected.delete(chat.id);
        updateSelection();
      });
      const body = element("span", "chat-catalog-main");
      body.append(element("strong", "", chat.title),
        element("small", "", `${basename(chat.workspace)} · ${date(chat.updated_at)}`));
      const detail = element("span", "chat-catalog-detail");
      const status = ({ active: "En cours", approval: "Validation", waiting: "Réponse attendue",
        failed: "Erreur", completed: "Terminé", interrupted: "Arrêté", stalled: "À vérifier",
        unconfirmed: "Activité non confirmée" })[chat.activity_status]
        || "État non confirmé";
      detail.append(element("span", `chat-catalog-status ${chat.activity_status || ""}`, archivedView() ? "Archivé" : status));
      const goal = chat.goal_status === "active" ? (chat.activity_status === "active" ? "Goal en cours" : "Goal ouvert") :
        ({ paused: "Goal en pause", blocked: "Goal bloqué", usageLimited: "Goal limité par quota",
          budgetLimited: "Goal limité par budget", complete: "Goal terminé" })[chat.goal_status];
      if (goal) detail.append(element("small", "", goal));
      detail.append(element("small", "", `${number(chat.tokens)} tokens cumulés`));
      row.append(box, body, detail);
      target.append(row);
    }
    byId("chats-range").textContent = chats ? `${chats.total ? offset + 1 : 0}–${Math.min(offset + chats.chats.length, chats.total)} sur ${number(chats.total)}` : "";
    byId("chat-filter-count").textContent = `${number(chats?.total || 0)} conversation${chats?.total === 1 ? "" : "s"}`;
    byId("chats-previous").disabled = offset === 0;
    byId("chats-next").disabled = !chats || offset + chats.chats.length >= chats.total;
    updateSelection();
  }

  function updateSelection() {
    byId("chat-selection").textContent = `${selected.size} sélectionné${selected.size > 1 ? "s" : ""}`;
    byId("chat-copy").disabled = selected.size === 0 || selected.size > 10 || catalog?.demo;
    byId("chat-copy").title = selected.size > 10 ? "10 chats maximum par copie" : "";
    byId("chat-copy").hidden = archivedView();
    byId("chat-target").parentElement.hidden = archivedView();
    byId("chat-archive").hidden = archivedView();
    byId("chat-restore").hidden = !archivedView();
    byId("chat-archive").disabled = selected.size === 0 || selected.size > 100 || catalog?.demo;
    byId("chat-restore").disabled = selected.size === 0 || selected.size > 100 || catalog?.demo;
    byId("chat-deduplicate").disabled = archivedView() || catalog?.demo;
  }

  async function loadChats() {
    const params = new URLSearchParams({ account_id: byId("chat-source").value,
      offset: String(offset), limit: "25", search: byId("chat-search").value,
      view: byId("chat-view").value,
      min_tokens: byId("chat-min-tokens").value,
      max_tokens: byId("chat-max-tokens").value,
      date_from: byId("chat-date-from").value,
      date_to: byId("chat-date-to").value });
    const response = await fetch(`/api/chats?${params}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    chats = payload;
    renderChats();
  }

  async function loadCatalog() {
    const response = await fetch("/api/environment-catalog", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    catalog = payload;
    const previous = byId("chat-source").value;
    const targetPrevious = byId("chat-target").value;
    accounts = payload.accounts;
    for (const select of [byId("chat-source"), byId("chat-target")]) {
      select.replaceChildren(...accounts.map((account) => {
        const option = element("option", "", account.name);
        option.value = account.id;
        return option;
      }));
    }
    byId("chat-source").value = previous || accounts[0]?.id || "";
    byId("chat-target").value = targetPrevious || accounts.find((account) => account.id !== byId("chat-source").value)?.id || "";
    renderInventory();
    await loadChats();
  }

  function message(text, error = false) {
    const target = byId("chat-message");
    target.textContent = text;
    target.classList.toggle("is-error", error);
  }

  async function post(path, payload) {
    const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  async function changeArchive(archive) {
    const ids = [...selected];
    if (!ids.length || ids.length > 100) return;
    const verb = archive ? "Archiver" : "Restaurer";
    if (!window.confirm(`${verb} ${ids.length} conversation${ids.length > 1 ? "s" : ""} du compte sélectionné ? L’historique reste conservé dans Codex.`)) return;
    message(`${verb} : opération en cours…`);
    try {
      const data = await post(`/api/chats/${archive ? "archive" : "unarchive"}`,
        { account_id: byId("chat-source").value, thread_ids: ids });
      const failed = data.results.filter((item) => item.status === "error");
      const done = data.results.length - failed.length;
      message(`${done} conversation${done > 1 ? "s" : ""} ${archive ? "archivée" : "restaurée"}${done > 1 ? "s" : ""}${failed.length ? ` · ${failed.length} échec(s) : ${failed.map((item) => item.message).join(" ; ")}` : ""}`, !!failed.length);
      selected = new Set(failed.map((item) => item.id));
      offset = 0;
      await loadCatalog();
    } catch (error) { message(error.message, true); }
  }

  async function deduplicate() {
    const accountId = byId("chat-source").value;
    try {
      const response = await fetch(`/api/chats/duplicates?account_id=${encodeURIComponent(accountId)}`, { cache: "no-store" });
      const plan = await response.json();
      if (!response.ok) throw new Error(plan.error || `HTTP ${response.status}`);
      if (!plan.groups.length) return message(plan.skipped_groups
        ? `Aucun groupe disponible. ${plan.skipped_groups} groupe(s) contiennent un chat actif ou épinglé.`
        : "Aucun doublon numéroté à archiver sur ce compte.");
      const preview = plan.groups.slice(0, 8).map((group) => `• ${group.title} : conserver le plus récent, archiver ${group.archive_count}`).join("\n");
      const extra = plan.groups.length > 8 ? `\n… et ${plan.groups.length - 8} autre(s) groupe(s).` : "";
      if (!window.confirm(`${plan.archive_count} anciens chats dans ${plan.groups.length} groupes seront archivés et les plus récents renommés.\n\n${preview}${extra}\n\nContinuer ?`)) return;
      message("Dédoublonnage dans Codex en cours…");
      const data = await post("/api/chats/deduplicate", { account_id: accountId });
      const archived = data.results.reduce((sum, item) => sum + item.archived, 0);
      const errors = data.results.flatMap((item) => item.errors || []);
      message(`${archived} ancien${archived > 1 ? "s" : ""} chat${archived > 1 ? "s" : ""} archivé${archived > 1 ? "s" : ""}, ${data.results.length} groupe(s) traité(s)${errors.length ? ` · ${errors.length} échec(s) : ${errors.map((item) => item.message).join(" ; ")}` : ""}`, !!errors.length);
      selected.clear(); offset = 0;
      await loadCatalog();
    } catch (error) { message(error.message, true); }
  }

  document.addEventListener("DOMContentLoaded", async () => {
    byId("chat-source").addEventListener("change", () => {
      selected = new Set(); offset = 0;
      byId("chat-target").value = accounts.find((item) => item.id !== byId("chat-source").value)?.id || "";
      loadChats().catch((error) => message(error.message, true));
    });
    byId("chat-search").addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => { selected.clear(); offset = 0; loadChats().catch((error) => message(error.message, true)); }, 250);
    });
    for (const id of ["chat-view", "chat-min-tokens", "chat-max-tokens", "chat-date-from", "chat-date-to"])
      byId(id).addEventListener(id.includes("tokens") ? "input" : "change", () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => { selected.clear(); offset = 0; loadChats().catch((error) => message(error.message, true)); }, 250);
      });
    byId("chat-clear-filters").addEventListener("click", () => {
      for (const id of ["chat-search", "chat-min-tokens", "chat-max-tokens", "chat-date-from", "chat-date-to"])
        byId(id).value = "";
      byId("chat-view").value = "active";
      selected.clear(); offset = 0;
      loadChats().catch((error) => message(error.message, true));
    });
    byId("chat-select-page").addEventListener("click", () => {
      const page = (chats?.chats || []).filter((item) => archivedView() || !["active", "approval", "waiting", "stalled"].includes(item.activity_status));
      const allSelected = page.length && page.every((item) => selected.has(item.id));
      for (const item of page) allSelected ? selected.delete(item.id) : selected.add(item.id);
      renderChats();
    });
    byId("chat-archive").addEventListener("click", () => changeArchive(true));
    byId("chat-restore").addEventListener("click", () => changeArchive(false));
    byId("chat-deduplicate").addEventListener("click", deduplicate);
    byId("chats-previous").addEventListener("click", () => {
      offset = Math.max(0, offset - 25); loadChats().catch((error) => message(error.message, true));
    });
    byId("chats-next").addEventListener("click", () => {
      offset += 25; loadChats().catch((error) => message(error.message, true));
    });
    byId("chat-copy").addEventListener("click", async () => {
      const source = byId("chat-source").value;
      const target = byId("chat-target").value;
      if (source === target) return message("Choisissez un compte de destination différent.", true);
      const ids = [...selected];
      if (!ids.length || ids.length > 10) return;
      if (!window.confirm(`Copier ${ids.length} chat${ids.length > 1 ? "s" : ""} vers ${accounts.find((item) => item.id === target)?.name} ? Les originaux restent dans le compte source.`)) return;
      byId("chat-copy").disabled = true;
      message("Copie et vérification Codex en cours…");
      try {
        const response = await fetch("/api/chats/transfer", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source, target, thread_ids: ids }) });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        const copied = data.results.filter((item) => item.status === "copied").length;
        const exists = data.results.filter((item) => item.status === "exists").length;
        const failed = data.results.filter((item) => item.status === "error");
        message(`${copied} copié${copied > 1 ? "s" : ""}${exists ? ` · ${exists} déjà présent${exists > 1 ? "s" : ""}` : ""}${failed.length ? ` · ${failed.length} échec${failed.length > 1 ? "s" : ""} : ${failed.map((item) => item.message).join(" ; ")}` : ""}`, !!failed.length);
        selected = new Set(failed.map((item) => item.id));
        await loadCatalog();
      } catch (error) { message(error.message, true); }
      updateSelection();
    });
    try { await loadCatalog(); } catch (error) { message(error.message, true); }
    setInterval(() => { if (catalog) loadCatalog().catch(() => {}); }, 60000);
  });
})();
