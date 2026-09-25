// Capture repeatable desktop/mobile demo screenshots with Chrome DevTools Protocol.
// Node 22+ and local Chrome are needed only to regenerate the documentation images.
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "..");
const dashboardUrl = process.env.CAPTURE_URL || "http://127.0.0.1:8768/";
const saveScreenshots = !process.env.CAPTURE_NO_SAVE;
const chromePath = process.env.CHROME_PATH || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "codex-manager-capture-"));
const chrome = spawn(chromePath, ["--headless=new", "--disable-gpu", "--no-first-run",
  "--remote-allow-origins=*", "--remote-debugging-port=0", `--user-data-dir=${profile}`,
  dashboardUrl], { stdio: "ignore", windowsHide: true });

async function waitFor(test, timeout = 10000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const result = await test();
    if (result) return result;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("Chrome DevTools indisponible");
}

async function main() {
  const port = await waitFor(() => {
    const file = path.join(profile, "DevToolsActivePort");
    return fs.existsSync(file) ? Number(fs.readFileSync(file, "utf8").split("\n")[0]) : null;
  });
  const page = await waitFor(async () => {
    try {
      const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      return targets.find((item) => item.type === "page" && item.url.startsWith(dashboardUrl));
    } catch { return null; }
  });
  const socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  let nextId = 0;
  const pending = new Map();
  const exceptions = [];
  socket.addEventListener("message", (message) => {
    const result = JSON.parse(message.data);
    if (result.method === "Runtime.exceptionThrown")
      exceptions.push(result.params?.exceptionDetails?.exception?.description ||
        result.params?.exceptionDetails?.text || "Erreur JavaScript");
    const waiter = pending.get(result.id);
    if (!waiter) return;
    pending.delete(result.id);
    result.error ? waiter.reject(new Error(result.error.message)) : waiter.resolve(result.result);
  });
  function call(method, params = {}) {
    const id = ++nextId;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      socket.send(JSON.stringify({ id, method, params }));
    });
  }
  await call("Page.enable");
  await call("Runtime.enable");
  if (process.env.CHECK_LIVE_WORKSPACE === "1") {
    await call("Page.navigate", { url: new URL("/#workspace-chat", dashboardUrl).href });
    await waitFor(async () => {
      const result = await call("Runtime.evaluate", { expression:
        "typeof workspaceGraph !== 'undefined' && workspaceGraph?.nodes().length >= 2 && workspaceGraph?.edges().length >= 2 && document.querySelectorAll('#workspace-message-list .workspace-chat-message').length >= 3", returnByValue: true });
      return result.result.value;
    }, 15000);
    const live = await call("Runtime.evaluate", { expression:
      "({available:workspaceState.agents.length,graphAgents:workspaceGraph.nodes().length,graphEdges:workspaceGraph.edges().length,graphSpacing:Math.abs(workspaceGraph.nodes()[0].renderedPosition('x')-workspaceGraph.nodes()[1].renderedPosition('x')),messages:document.querySelectorAll('#workspace-message-list .workspace-chat-message').length,reply:workspaceState.messages.some(item=>item.reply_to_id && workspaceState.messages.some(parent=>parent.id===item.reply_to_id && parent.target_agent_id===item.sender_agent_id)),errors:document.getElementById('workspace-launch-status').textContent})", returnByValue: true });
    if (!live.result.value.reply || live.result.value.graphSpacing < 160 || exceptions.length)
      throw new Error(`Échange réel non affiché : ${JSON.stringify(live.result.value)} · ${exceptions.join(" | ")}`);
    process.stdout.write(`live Workspace MCP: ${JSON.stringify(live.result.value)}\n`);
    if (process.env.CHECK_LIVE_DIAGNOSTICS === "1") {
      await call("Runtime.evaluate", { expression:
        "document.querySelector('#workspace-agent-list button')?.click(); document.getElementById('workspace-check-button').click()" });
      await waitFor(async () => {
        const result = await call("Runtime.evaluate", { expression:
          "document.querySelectorAll('#workspace-check-results .workspace-check-item').length >= 5", returnByValue: true });
        return result.result.value;
      }, 15000);
      const check = await call("Runtime.evaluate", { expression:
        "({rows:[...document.querySelectorAll('#workspace-check-results .workspace-check-item strong')].map(item=>item.textContent),error:document.getElementById('workspace-check-summary').textContent})", returnByValue: true });
      if (exceptions.length || check.result.value.error.includes("impossible"))
        throw new Error(`Diagnostic interactif : ${JSON.stringify(check.result.value)} · ${exceptions.join(" | ")}`);
      process.stdout.write(`diagnostic interactif: ${JSON.stringify(check.result.value)}\n`);
      await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844,
        deviceScaleFactor: 1, mobile: true });
      const mobileCheck = await call("Runtime.evaluate", { expression:
        "({width:innerWidth,scrollWidth:document.documentElement.scrollWidth})", returnByValue: true });
      if (mobileCheck.result.value.scrollWidth > mobileCheck.result.value.width + 1)
        throw new Error(`Diagnostic mobile déborde : ${JSON.stringify(mobileCheck.result.value)}`);
    }
    if (process.env.CHECK_LIVE_SCREENSHOT === "1") {
      await call("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1100,
        deviceScaleFactor: 1, mobile: false });
      const screenshot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      fs.writeFileSync(path.join(root, "data", "workspace-live-check.png"), Buffer.from(screenshot.data, "base64"));
    }
    if (process.env.CHECK_LIVE_SEND === "1") {
      const sent = await call("Runtime.evaluate", { expression: `(() => {
        window.__workspaceSentAfter = Math.max(0, ...workspaceState.messages.map((item) => item.id));
        const select = document.getElementById('workspace-recipient');
        const option = [...select.options].find((item) => item.value);
        if (!option) return false;
        select.value = option.value;
        select.dispatchEvent(new Event('change'));
        document.getElementById('workspace-input').value = 'Test depuis le dashboard : confirme brièvement que tu vois ce message.';
        document.getElementById('workspace-compose').requestSubmit();
        return true;
      })()`, returnByValue: true });
      if (!sent.result.value) throw new Error("Aucun agent CLI disponible dans le formulaire");
      await waitFor(async () => {
        const result = await call("Runtime.evaluate", { expression:
          "workspaceState.messages.some(item=>item.reply_to_id>window.__workspaceSentAfter && workspaceState.messages.some(parent=>parent.id===item.reply_to_id && parent.sender_agent_id==='workspace:operator'))", returnByValue: true });
        return result.result.value;
      }, 45000);
      process.stdout.write("dashboard → MCP → Codex CLI → MCP → chat : OK\n");
    }
    socket.close();
    return;
  }
  for (const [name, width, height, mobile, dark, save] of [
    ["desktop", 1440, 1100, false, false, true],
    ["desktop-dark", 1440, 1100, false, true, true],
    ["mobile", 390, 1000, true, false, true],
    ["mobile-320", 320, 1000, true, false, false],
  ]) {
    await call("Emulation.setDeviceMetricsOverride", {
      width, height, deviceScaleFactor: 1, mobile,
    });
    await call("Page.reload", { ignoreCache: true });
    await waitFor(async () => {
      const result = await call("Runtime.evaluate", { expression:
        "(() => { const link=document.querySelector('.sidebar-nav a[href=\"#overview\"]'); if (!link) return false; link.click(); return true; })()", returnByValue: true });
      return result.result.value;
    });
    await waitFor(async () => {
      const result = await call("Runtime.evaluate", { expression:
        "document.querySelectorAll('#accounts .account-section').length === 2 && document.querySelectorAll('#project-usage-accounts .project-usage-account').length === 2 && document.querySelectorAll('#catalog-accounts .catalog-account').length === 2 && document.querySelectorAll('#preferences-accounts fieldset').length === 2", returnByValue: true });
      return result.result.value;
    }).catch(async (error) => {
      const state = await call("Runtime.evaluate", { expression:
        "({accounts:document.querySelectorAll('#accounts .account-section').length,projects:document.querySelectorAll('#project-usage-accounts .project-usage-account').length,catalog:document.querySelectorAll('#catalog-accounts .catalog-account').length,preferences:document.querySelectorAll('#preferences-accounts fieldset').length,ready:document.readyState,workspace:location.hash})", returnByValue: true });
      throw new Error(`${error.message} : ${JSON.stringify(state.result.value)} · ${exceptions.join(" | ")}`);
    });
    if (dark) await call("Runtime.evaluate", { expression: "document.documentElement.dataset.theme = 'dark'" });
    const dimensions = await call("Runtime.evaluate", { expression:
      "({width:window.innerWidth,scrollWidth:document.documentElement.scrollWidth})", returnByValue: true });
    if (dimensions.result.value.scrollWidth > dimensions.result.value.width + 1)
      throw new Error(`${name} : débordement horizontal ${JSON.stringify(dimensions.result.value)}`);
    if (name === "desktop") {
      const alignment = await call("Runtime.evaluate", { expression: `(() => {
        const sections = [...document.querySelectorAll('#accounts .account-section')];
        const rows = ['.account-head', '.metrics-row', '.quota-outlook', '.usage-details', '.list-heading'];
        return rows.map((selector) => {
          const positions = sections.map((section) => section.querySelector(selector).getBoundingClientRect().top);
          return { selector, difference: Math.abs(positions[0] - positions[1]) };
        });
      })()`, returnByValue: true });
      if (alignment.result.value.some((row) => row.difference > 1))
        throw new Error(`Colonnes désalignées : ${JSON.stringify(alignment.result.value)}`);
    }
    if (save && saveScreenshots) {
      const screenshot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      fs.writeFileSync(path.join(root, "demo", `dashboard-${name}.png`), Buffer.from(screenshot.data, "base64"));
      if (name === "desktop" || name === "mobile") {
        for (const [section, filename] of [["project-usage", "project-usage"], ["catalog", "catalog"]]) {
          await call("Runtime.evaluate", { expression: `window.scrollTo({top: document.getElementById('${section}').getBoundingClientRect().top + window.scrollY - 16, behavior: 'instant'})` });
          await new Promise((resolve) => setTimeout(resolve, 120));
          const sectionScreenshot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
          fs.writeFileSync(path.join(root, "demo", `${filename}-${name}.png`), Buffer.from(sectionScreenshot.data, "base64"));
        }
        await call("Runtime.evaluate", { expression: "document.querySelector('.sidebar-nav a[href=\"#workspace-chat\"]').click()" });
        await waitFor(async () => {
          const result = await call("Runtime.evaluate", { expression:
            "typeof workspaceGraph !== 'undefined' && workspaceGraph?.nodes().length === 2", returnByValue: true });
          return result.result.value;
        });
        const workspaceScreenshot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
        fs.writeFileSync(path.join(root, "demo", `workspace-mcp-${name}.png`), Buffer.from(workspaceScreenshot.data, "base64"));
      }
    }
    process.stdout.write(`${name}: ${JSON.stringify(dimensions.result.value)}\n`);
  }
  await call("Page.navigate", { url: new URL("/?route-check=1#workspace-kanban", dashboardUrl).href });
  await waitFor(async () => {
    const result = await call("Runtime.evaluate", { expression:
      "!document.getElementById('workspace-page')?.hidden && document.querySelectorAll('#workspace-kanban-board .workspace-kanban-column').length===5 && document.querySelectorAll('#workspace-kanban-board .workspace-task-card').length===3", returnByValue: true });
    return result.result.value;
  });
  await call("Runtime.evaluate", { expression:
    "document.querySelector('#workspace-kanban-board .workspace-task-card button').click()" });
  await waitFor(async () => {
    const result = await call("Runtime.evaluate", { expression:
      "!document.getElementById('workspace-task-detail').hidden && document.querySelectorAll('#workspace-task-events .workspace-task-event').length>0", returnByValue: true });
    return result.result.value;
  });
  if (process.env.CHECK_KANBAN_SCREENSHOT === "1") {
    await call("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1100,
      deviceScaleFactor: 1, mobile: false });
    const shot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    fs.writeFileSync(path.join(root, "data", "workspace-kanban-check.png"), Buffer.from(shot.data, "base64"));
  }
  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844,
    deviceScaleFactor: 1, mobile: true });
  const kanbanMobile = await call("Runtime.evaluate", { expression:
    "({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,boardWidth:document.getElementById('workspace-kanban-board').clientWidth,boardScrollWidth:document.getElementById('workspace-kanban-board').scrollWidth})", returnByValue: true });
  if (kanbanMobile.result.value.scrollWidth > kanbanMobile.result.value.width + 1 ||
      kanbanMobile.result.value.boardScrollWidth <= kanbanMobile.result.value.boardWidth)
    throw new Error(`Kanban mobile : ${JSON.stringify(kanbanMobile.result.value)}`);
  await call("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1100,
    deviceScaleFactor: 1, mobile: false });
  process.stdout.write(`kanban: 5 colonnes, 3 tâches, détail et mobile OK\n`);
  await waitFor(async () => {
    const result = await call("Runtime.evaluate", { expression:
      "(() => { const link=document.querySelector('.sidebar-nav a[href=\"#workspace-chat\"]'); if (!link) return false; link.click(); return true; })()", returnByValue: true });
    return result.result.value;
  });
  await waitFor(async () => {
    const result = await call("Runtime.evaluate", { expression:
      "!document.getElementById('workspace-page')?.hidden && document.getElementById('dashboard-page')?.hidden && typeof workspaceGraph !== 'undefined' && workspaceGraph?.nodes().length === 2 && document.querySelectorAll('#workspace-conversation-list .workspace-conversation-item').length === 2 && document.querySelectorAll('#sidebar-accounts a').length === 2", returnByValue: true });
    return result.result.value;
  });
  await waitFor(async () => {
    const result = await call("Runtime.evaluate", { expression:
      "document.querySelectorAll('#workspace-message-list .workspace-chat-message').length === 3", returnByValue: true });
    return result.result.value;
  });
  await call("Runtime.evaluate", { expression:
    "window.__workspaceSignatureBefore = workspaceState.viewSignature; window.__workspaceMutations = []; window.__workspaceObserver = new MutationObserver((items) => window.__workspaceMutations.push(...items.map((item) => ({type:item.type,target:item.target.id || item.target.className,attribute:item.attributeName})))); window.__workspaceObserver.observe(document.querySelector('.app-shell'), {subtree:true,childList:true,characterData:true,attributes:true})" });
  await new Promise((resolve) => setTimeout(resolve, 4500));
  const stable = await call("Runtime.evaluate", { expression:
    "window.__workspaceObserver.disconnect(); window.__workspaceMutations", returnByValue: true });
  const disruptiveMutations = stable.result.value.filter((item) => item.type === "childList" &&
    /workspace-(agent-list|message-list|conversation-list|kanban-board)/.test(item.target));
  if (disruptiveMutations.length)
    throw new Error(`Workspace remonté sans changement de données : ${JSON.stringify(disruptiveMutations)} · ` +
      JSON.stringify((await call("Runtime.evaluate", { expression:
        "({signatureChanged:window.__workspaceSignatureBefore!==workspaceState.viewSignature,changedParts:JSON.parse(window.__workspaceSignatureBefore).map((part,index)=>JSON.stringify(part)===JSON.stringify(JSON.parse(workspaceState.viewSignature)[index])?null:index).filter(index=>index!==null)})", returnByValue: true })).result.value));
  const incremental = await call("Runtime.evaluate", { expression: `(() => {
    const original = structuredClone(workspaceState.overview);
    const retainedId = workspaceGraph.nodes().first().id();
    const removedId = workspaceGraph.nodes().last().id();
    const firstAgent = [...document.querySelectorAll('#workspace-agent-list .workspace-agent-item')]
      .find((item) => item.dataset.itemKey === retainedId);
    const firstGraphAgent = workspaceGraph.getElementById(retainedId)[0];
    const firstMessage = document.querySelector('#workspace-message-list .workspace-chat-message');
    const reduced = structuredClone(original);
    reduced.agents = reduced.agents.filter((item) => item.id !== removedId);
    reduced.graph_agents = reduced.graph_agents.filter((item) => item.id !== removedId);
    reduced.connections = reduced.connections.filter((item) => item.sender !== removedId && item.recipient !== removedId);
    reduced.channels.find((channel) => channel.id === 'general').members =
      reduced.agents.map((agent) => agent.id);
    reduced.channels.find((channel) => channel.id === 'general').participants = 2;
    renderWorkspaceMcp(reduced);
    const correctCount = document.querySelectorAll('#workspace-agent-list .workspace-agent-item').length === 2;
    const stableAgent = firstAgent === [...document.querySelectorAll('#workspace-agent-list .workspace-agent-item')]
      .find((item) => item.dataset.itemKey === retainedId);
    const stableGraph = firstGraphAgent === workspaceGraph.getElementById(retainedId)[0];
    const stableMessage = firstMessage === document.querySelector('#workspace-message-list .workspace-chat-message');
    renderWorkspaceMcp(original);
    return {correctCount,stableAgent,stableGraph,stableMessage,restored:
      document.querySelectorAll('#workspace-agent-list .workspace-agent-item').length === 3};
  })()`, returnByValue: true });
  if (Object.values(incremental.result.value).some((value) => !value))
    throw new Error(`Mise à jour incrémentale incorrecte : ${JSON.stringify(incremental.result.value)}`);
  await call("Runtime.evaluate", { expression:
    "document.querySelectorAll('#workspace-conversation-list .workspace-conversation-item')[1].click()" });
  await waitFor(async () => {
    const result = await call("Runtime.evaluate", { expression:
      "document.querySelectorAll('#workspace-message-list .workspace-chat-message').length === 3 && document.querySelectorAll('#workspace-channel-members .workspace-member').length === 2", returnByValue: true });
    return result.result.value;
  });
  const interaction = await call("Runtime.evaluate", { expression: `(async () => {
    const before = document.documentElement.dataset.theme;
    document.getElementById('theme-toggle').click();
    document.querySelector('.sidebar-nav a[href="#workspace-chat"]').click();
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const workspaceOpened = !document.getElementById('workspace-page').hidden && document.getElementById('dashboard-page').hidden;
    workspaceGraph.nodes().first().emit('tap');
    const agentSelected = workspaceGraph.nodes().first().hasClass('is-selected');
    const graphSpacing = Math.abs(workspaceGraph.nodes()[0].renderedPosition('x') - workspaceGraph.nodes()[1].renderedPosition('x'));
    document.querySelector('.sidebar-nav a[href="#preferences-panel"]').click();
    return {
      themeChanged: document.documentElement.dataset.theme !== before,
      workspaceOpened,
      agentSelected,
      dashboardReturned: !document.getElementById('dashboard-page').hidden && document.getElementById('workspace-page').hidden,
      preferencesOpened: document.getElementById('preferences-panel').open,
      accountLinks: document.querySelectorAll('#sidebar-accounts a').length,
      navigationIcons: document.querySelectorAll('.sidebar-nav .sidebar-link .nav-icon').length,
      navigationWithoutUnicodeIcons: document.querySelectorAll('.sidebar-nav .sidebar-link .nav-icon:not(svg)').length === 0,
      workspaceIconVisible: [...document.querySelectorAll('.sidebar-workspace-items .nav-icon')].length === 2 && [...document.querySelectorAll('.sidebar-workspace-items .nav-icon')].every((icon) => getComputedStyle(icon).display !== 'none'),
      integrationLogos: document.querySelectorAll('#integrations img').length,
      telegramActive: !document.getElementById('integration-telegram').classList.contains('is-inactive'),
      futureIntegrationsInactive: [...document.querySelectorAll('#integrations .integration-row')].slice(1).every((row) => row.classList.contains('is-inactive')),
      graphAgents: workspaceGraph.nodes().length,
      graphConnections: workspaceGraph.edges().length,
      graphSpacing,
      availableAgents: document.querySelectorAll('#workspace-agent-list .workspace-agent-item').length,
      conversationRows: document.querySelectorAll('#workspace-conversation-list .workspace-conversation-item').length,
      renderedMessages: document.querySelectorAll('#workspace-message-list .workspace-chat-message').length,
    };
  })()`, returnByValue: true, awaitPromise: true });
  const checks = interaction.result.value;
  checks.renderedMessages = await waitFor(async () => {
    const result = await call("Runtime.evaluate", { expression:
      "document.querySelectorAll('#workspace-message-list .workspace-chat-message').length", returnByValue: true });
    return result.result.value >= 3 ? result.result.value : null;
  });
  const health = await (await fetch(new URL('/api/health', dashboardUrl))).json();
  const expectedTelegram = Boolean(health.telegram_goal_alerts || health.telegram_answer_alerts || health.telegram_action_alerts || health.telegram_metric_alerts);
  if (!checks.themeChanged || !checks.workspaceOpened || !checks.agentSelected || !checks.dashboardReturned || !checks.preferencesOpened || checks.accountLinks !== 2 || checks.navigationIcons !== 9 || !checks.navigationWithoutUnicodeIcons || !checks.workspaceIconVisible || checks.integrationLogos !== 3 || checks.telegramActive !== expectedTelegram || !checks.futureIntegrationsInactive || checks.graphAgents !== 2 || checks.graphConnections !== 2 || checks.graphSpacing < 160 || checks.availableAgents !== 3 || checks.conversationRows !== 2 || checks.renderedMessages < 3)
    throw new Error(`Interactions du dashboard : ${JSON.stringify(checks)}`);
  process.stdout.write(`interactions: ${JSON.stringify(checks)}\n`);
  socket.close();
}

try {
  await main();
} finally {
  chrome.kill();
  const resolved = path.resolve(profile);
  const tempRoot = path.resolve(os.tmpdir()) + path.sep;
  if (resolved.startsWith(tempRoot)) {
    try { fs.rmSync(resolved, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 }); }
    catch { /* Windows may keep the headless browser profile locked briefly. */ }
  }
}
