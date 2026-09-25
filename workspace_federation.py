"""One shared Workspace channel over a private Tailscale link.

The host owns the history. The client owns a durable outgoing queue. Tokens are
read from the environment, never stored in the configuration file or responses.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from workspace_mailbox import DEFAULT_DB, Mailbox, valid_address

DEFAULT_CONFIG = Path(__file__).resolve().parent / "data" / "workspace-federation.json"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # Never forward the peer bearer token to a different destination.
        return None


_PEER_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def load_config(path: Path = DEFAULT_CONFIG) -> dict | None:
    if not path.is_file():
        return None
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("role") not in {"host", "client"}:
        raise ValueError("Rôle de fédération invalide")
    for key in ("workspace_id", "peer_id", "channel_id"):
        valid_address(config[key])
    if config["workspace_id"] == config["peer_id"]:
        raise ValueError("Identifiants de workspace identiques")
    if not config["channel_id"].startswith("shared:"):
        raise ValueError("Channel partagé invalide")
    if not isinstance(config.get("channel_name"), str) or not config["channel_name"].strip():
        raise ValueError("Nom de channel manquant")
    if config.get("token_env", "CODEX_WORKSPACE_PEER_TOKEN") not in os.environ:
        raise ValueError("Variable d'environnement du pair absente")
    token = os.environ[config.get("token_env", "CODEX_WORKSPACE_PEER_TOKEN")]
    if len(token) < 32:
        raise ValueError("Jeton de pair trop court")
    if config["role"] == "host":
        bind = ipaddress.ip_address(config["bind"])
        if not isinstance(bind, ipaddress.IPv4Address) or not (
                bind.is_loopback or bind in ipaddress.ip_network("100.64.0.0/10")):
            raise ValueError("La passerelle V1 doit écouter sur une adresse IPv4 Tailscale")
        port = int(config.get("port", 8766))
        if not 1024 <= port <= 65535:
            raise ValueError("Port invalide")
        dashboard_port = int(config.get("dashboard_port", 8765))
        if not 1024 <= dashboard_port <= 65535:
            raise ValueError("Port du dashboard invalide")
    else:
        url = urllib.parse.urlparse(config["peer_url"])
        if (url.scheme != "http" or not url.hostname or url.username or url.password
                or url.path not in ("", "/") or url.query or url.fragment):
            raise ValueError("URL de pair invalide")
        try:
            host = ipaddress.ip_address(url.hostname)
            if not isinstance(host, ipaddress.IPv4Address) or not (
                    host.is_loopback or host in ipaddress.ip_network("100.64.0.0/10")):
                raise ValueError("Le pair doit être joignable par Tailscale")
        except ValueError as exc:
            raise ValueError("Utilisez l'adresse IP Tailscale du pair, sans nom DNS") from exc
        try:
            port = url.port
        except ValueError as exc:
            raise ValueError("Port du pair invalide") from exc
        if port is None or not 1024 <= port <= 65535:
            raise ValueError("Port du pair invalide")
    return config


def prepare_channel(config: dict, mailbox: Mailbox):
    mailbox.create_shared_channel(config["channel_id"], config["channel_name"])


def _remote_id(config: dict, local_agent_id: str) -> str:
    return valid_address(f"{config['peer_id']}:{valid_address(local_agent_id)}")


def _globalize_from_client(config: dict, agent_id: str) -> str:
    agent_id = valid_address(agent_id)
    host_prefix = config["workspace_id"] + ":"
    return agent_id[len(host_prefix):] if agent_id.startswith(host_prefix) else _remote_id(config, agent_id)


def _localize_id(config: dict, agent_id: str | None) -> str | None:
    if not agent_id:
        return agent_id
    own_prefix = config["workspace_id"] + ":"
    if agent_id.startswith(own_prefix):
        return agent_id[len(own_prefix):]
    return valid_address(f"{config['peer_id']}:{agent_id}")


def _globalize_id(config: dict, agent_id: str | None) -> str | None:
    if not agent_id:
        return agent_id
    peer_prefix = config["peer_id"] + ":"
    if agent_id.startswith(peer_prefix):
        return agent_id[len(peer_prefix):]
    return valid_address(f"{config['workspace_id']}:{agent_id}")


def localize_message(config: dict, message: dict) -> dict:
    item = dict(message)
    for key in ("sender_agent_id", "target_agent_id"):
        item[key] = _localize_id(config, item.get(key))
    if "deliveries" in item:
        item["deliveries"] = [{**delivery,
            "agent_id": _localize_id(config, delivery["agent_id"])}
            for delivery in item["deliveries"]]
    return item


def public_diagnostics(result: dict) -> dict:
    labels = {"ok": "Vérifié par le workspace distant",
              "incomplete": "Incomplet d'après le workspace distant",
              "error": "Erreur signalée par le workspace distant",
              "unknown": "Non vérifié par le workspace distant"}
    checks = {}
    for key in ("github", "github_mcp", "mcp", "hooks", "delivery"):
        source = result.get(key) or {}
        status = source.get("status")
        status = status if status in labels else "unknown"
        checks[key] = {"status": status, "detail": labels[status], "at": source.get("at")}
    return {"checked_at": result.get("checked_at", ""), **checks}


def shared_overview(mailbox: Mailbox, channel_id: str) -> dict:
    overview = mailbox.overview()
    channel = next((item for item in overview["channels"] if item["id"] == channel_id), None)
    if not channel:
        raise ValueError("Channel introuvable")
    members = set(mailbox.shared_members(channel_id))
    def public_agent(agent: dict) -> dict:
        return {**agent, "workspace": Path(agent.get("workspace") or "").name}
    return {"channel": channel,
            "agents": [public_agent(agent) for agent in overview["agents"] if agent["id"] in members],
            "directory": [public_agent(agent) for agent in overview["directory"] if agent["id"] in members],
            "connections": [edge for edge in overview["connections"] if edge["channel_id"] == channel_id],
            "graph_agents": [public_agent(agent) for agent in overview["graph_agents"] if agent["id"] in members],
            "agent_summaries": {identity: summary for identity, summary in overview["agent_summaries"].items()
                                if identity in members},
            "graph_activity": [item for item in overview["graph_activity"] if item["channel_id"] == channel_id]}


class PeerHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def reply(self, code: int, value: dict):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        expected = os.environ.get(self.server.config.get("token_env", "CODEX_WORKSPACE_PEER_TOKEN"), "")
        provided = self.headers.get("Authorization", "")
        allowed = bool(expected) and hmac.compare_digest(provided, "Bearer " + expected)
        if allowed:
            self.server.last_contact = time.time()
        return allowed

    def do_GET(self):
        if not self.authorized():
            return self.reply(401, {"error": "Accès refusé"})
        path = urllib.parse.urlparse(self.path).path
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        mailbox = Mailbox(self.server.db_path)
        try:
            channel_id = self.server.config["channel_id"]
            if path == "/overview":
                return self.reply(200, {"workspace_id": self.server.config["workspace_id"],
                                        **shared_overview(mailbox, channel_id)})
            if path == "/channel":
                before = int(query.get("before_id", ["0"])[0])
                limit = int(query.get("limit", ["50"])[0])
                agent_raw = query.get("agent_id", [""])[0]
                agent_id = _remote_id(self.server.config, agent_raw) if agent_raw else None
                first = query.get("sender", [""])[0]
                second = query.get("recipient", [""])[0]
                pair = (_globalize_from_client(self.server.config, first),
                        _globalize_from_client(self.server.config, second)) if first and second else None
                value = mailbox.messages(channel_id, before, limit, agent_id, pair)
                return self.reply(200, value)
            if path == "/inbox":
                agent_id = _remote_id(self.server.config, query.get("agent_id", [""])[0])
                mailbox._check_member(agent_id, channel_id)
                after_id = int(query.get("after_id", ["0"])[0])
                limit = int(query.get("limit", ["12"])[0])
                return self.reply(200, {"messages": mailbox.pending(agent_id, limit=limit,
                                                                     after_id=after_id)})
            if path == "/message":
                agent_id = _remote_id(self.server.config, query.get("agent_id", [""])[0])
                return self.reply(200, {"message": mailbox.message(int(query.get("message_id", ["0"])[0]), agent_id)})
            if path == "/agent":
                raw_agent = query.get("agent_id", [""])[0]
                agent_id = (valid_address(raw_agent) if query.get("scope", [""])[0] == "host"
                            else _remote_id(self.server.config, raw_agent))
                mailbox._check_member(agent_id, channel_id)
                return self.reply(200, {"messages": [item for item in mailbox.agent_history(agent_id)
                                                     if item["channel_id"] == channel_id]})
            if path == "/diagnostics":
                agent_id = valid_address(query.get("agent_id", [""])[0])
                mailbox._check_member(agent_id, channel_id)
                if agent_id.startswith(self.server.config["peer_id"] + ":"):
                    raise ValueError("Diagnostic local requis pour cet agent")
                target = (f"http://127.0.0.1:{self.server.config.get('dashboard_port', 8765)}/api/workspace-mcp/diagnostics?"
                          + urllib.parse.urlencode({"agent_id": agent_id}))
                with urllib.request.urlopen(target, timeout=5) as response:
                    return self.reply(200, public_diagnostics(json.load(response)))
            return self.reply(404, {"error": "Introuvable"})
        except (ValueError, TypeError) as exc:
            return self.reply(400, {"error": str(exc)[:180]})
        except OSError:
            return self.reply(503, {"error": "Diagnostic ou channel momentanément indisponible"})
        finally:
            mailbox.close()

    def do_POST(self):
        if not self.authorized():
            return self.reply(401, {"error": "Accès refusé"})
        path = urllib.parse.urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.reply(400, {"error": "Longueur de requête invalide"})
        if length < 1 or length > 32768 or self.headers.get("Content-Type") != "application/json":
            return self.reply(400, {"error": "Requête invalide"})
        try:
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Objet JSON requis")
            mailbox = Mailbox(self.server.db_path)
            try:
                config = self.server.config
                channel_id = config["channel_id"]
                if path == "/presence":
                    agents = data.get("agents")
                    if not isinstance(agents, list) or len(agents) > 30:
                        raise ValueError("Liste d'agents invalide")
                    for agent in agents:
                        if not isinstance(agent, dict):
                            raise ValueError("Agent invalide")
                        registered = mailbox.register_remote_agent(config["peer_id"], agent["id"],
                            channel_id, agent.get("name", ""), agent.get("status", "idle"))
                        if agent.get("diagnostics") is not None:
                            mailbox.save_remote_diagnostics(registered["id"], agent["diagnostics"])
                    return self.reply(200, {"ok": True})
                if path == "/send":
                    sender = _remote_id(config, data.get("sender_agent_id", ""))
                    mailbox._check_member(sender, channel_id)
                    target = data.get("target_agent_id")
                    if target:
                        mailbox._check_member(target, channel_id)
                    message = mailbox.send(sender, data.get("content"), channel_id,
                                           target_agent=target, reply_to_id=data.get("reply_to_id"),
                                           client_key=data.get("client_key"), references=data.get("references"))
                    return self.reply(200, {"message": message})
                if path == "/delivered":
                    agent = _remote_id(config, data.get("agent_id", ""))
                    mailbox._check_member(agent, channel_id)
                    ids = data.get("message_ids")
                    if not isinstance(ids, list) or len(ids) > 50 or any(type(i) is not int for i in ids):
                        raise ValueError("Messages invalides")
                    mailbox.mark_delivered(agent, ids, data.get("method", "hook"))
                    return self.reply(200, {"ok": True})
                return self.reply(404, {"error": "Introuvable"})
            finally:
                mailbox.close()
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self.reply(400, {"error": str(exc)[:180]})


class PeerServer(ThreadingHTTPServer):
    daemon_threads = True


def start_host(config: dict, db_path: Path = DEFAULT_DB) -> PeerServer:
    if config["role"] != "host":
        raise ValueError("Ce workspace n'est pas l'hôte")
    mailbox = Mailbox(db_path)
    try:
        prepare_channel(config, mailbox)
    finally:
        mailbox.close()
    server = PeerServer((config["bind"], int(config.get("port", 8766))), PeerHandler)
    server.config = config
    server.db_path = db_path
    server.last_contact = 0.0
    return server


class PeerClient:
    def __init__(self, config: dict, timeout: float = 3):
        if config["role"] != "client":
            raise ValueError("Ce workspace n'est pas client")
        self.config = config
        self.timeout = timeout

    def request(self, path: str, payload: dict | None = None) -> dict:
        url = self.config["peer_url"].rstrip("/") + path
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        token = os.environ[self.config.get("token_env", "CODEX_WORKSPACE_PEER_TOKEN")]
        request = urllib.request.Request(url, data=data,
            headers={"Authorization": "Bearer " + token,
                     **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with _PEER_OPENER.open(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise ValueError(f"Passerelle distante : HTTP {exc.code}") from exc

    def overview(self) -> dict:
        result = self.request("/overview")
        config = self.config
        def agent(item):
            return {**item, "id": _localize_id(config, item["id"]), "remote":
                    not item["id"].startswith(config["workspace_id"] + ":")}
        result["agents"] = [agent(item) for item in result["agents"]]
        result["directory"] = [agent(item) for item in result["directory"]]
        result["graph_agents"] = [agent(item) for item in result["graph_agents"]]
        result["channel"]["members"] = [_localize_id(config, item) for item in result["channel"]["members"]]
        for edge in result["connections"]:
            edge["sender"] = _localize_id(config, edge["sender"])
            edge["recipient"] = _localize_id(config, edge["recipient"])
        for item in result["graph_activity"]:
            item["agent_id"] = _localize_id(config, item["agent_id"])
        result["agent_summaries"] = {_localize_id(config, identity): summary
                                     for identity, summary in result["agent_summaries"].items()}
        return result

    def messages(self, before_id: int = 0, limit: int = 50, agent_id: str | None = None,
                 pair: tuple[str, str] | None = None) -> dict:
        query = urllib.parse.urlencode({"before_id": before_id, "limit": limit,
                                       **({"agent_id": agent_id} if agent_id else {}),
                                       **({"sender": pair[0], "recipient": pair[1]} if pair else {})})
        result = self.request("/channel?" + query)
        result["messages"] = [localize_message(self.config, item) for item in result["messages"]]
        return result

    def inbox(self, agent_id: str, after_id: int = 0, limit: int = 12) -> list[dict]:
        query = urllib.parse.urlencode({"agent_id": valid_address(agent_id),
                                       "after_id": after_id, "limit": limit})
        return [localize_message(self.config, item) for item in self.request("/inbox?" + query)["messages"]]

    def mark_delivered(self, agent_id: str, message_ids: list[int], method: str = "hook"):
        if method not in {"hook", "mcp"}:
            raise ValueError("Méthode de livraison invalide")
        agent_id = valid_address(agent_id)
        for start in range(0, len(message_ids), 50):
            self.request("/delivered", {"agent_id": agent_id,
                "message_ids": message_ids[start:start + 50], "method": method})

    def message(self, agent_id: str, message_id: int) -> dict:
        query = urllib.parse.urlencode({"agent_id": agent_id, "message_id": message_id})
        return localize_message(self.config, self.request("/message?" + query)["message"])

    def agent_history(self, agent_id: str) -> list[dict]:
        agent_id = valid_address(agent_id)
        prefix = self.config["peer_id"] + ":"
        host_agent = agent_id.startswith(prefix)
        query = urllib.parse.urlencode({"agent_id": agent_id[len(prefix):] if host_agent else agent_id,
                                       **({"scope": "host"} if host_agent else {})})
        return [localize_message(self.config, item)
                for item in self.request("/agent?" + query)["messages"]]

    def diagnostics(self, agent_id: str) -> dict:
        prefix = self.config["peer_id"] + ":"
        if not valid_address(agent_id).startswith(prefix):
            raise ValueError("Agent distant requis")
        query = urllib.parse.urlencode({"agent_id": agent_id[len(prefix):]})
        result = self.request("/diagnostics?" + query)
        result["agent_id"] = agent_id
        result["origin"] = "workspace_distant"
        result["origin_workspace"] = self.config["peer_id"]
        result["reported_at"] = result.get("checked_at")
        return result

    def announce(self, agents: list[dict]):
        self.request("/presence", {"agents": [{"id": item["id"], "name": item.get("name", ""),
            "status": item.get("status", "idle"),
            **({"diagnostics": item["diagnostics"]} if item.get("diagnostics") else {})}
            for item in agents]})

    def flush(self, mailbox: Mailbox) -> int:
        sent = 0
        for item in mailbox.queued_federated(self.config["channel_id"]):
            try:
                message = self.request("/send", {"sender_agent_id": item["sender_agent_id"],
                    "content": item["content"], "target_agent_id": _globalize_id(self.config, item["target_agent_id"]),
                    "reply_to_id": item["reply_to_id"], "client_key": item["client_key"],
                    "references": json.loads(item["references_json"])})["message"]
                mailbox.mark_federated_sent(item["client_key"], message["id"])
                sent += 1
            except (OSError, ValueError, urllib.error.URLError) as exc:
                mailbox.mark_federated_error(item["client_key"], str(exc))
                break
        return sent

    def sync(self, mailbox: Mailbox, store=None) -> int:
        prepare_channel(self.config, mailbox)
        members = set(mailbox.shared_members(self.config["channel_id"]))
        agents = [agent for agent in mailbox.active_agents() if agent["id"] in members and
                  ":" in agent["id"] and not agent["account_id"].count(":")]
        if store:
            from workspace_diagnostics import diagnose
            for agent in agents:
                try:
                    agent["diagnostics"] = public_diagnostics(
                        diagnose(store, agent["id"], mailbox, probe_host=False))
                except (OSError, ValueError):
                    pass
        self.announce(agents)
        return self.flush(mailbox)


def poll_client(config: dict, db_path: Path = DEFAULT_DB, interval: int = 8, store=None):
    client = PeerClient(config)
    while True:
        mailbox = Mailbox(db_path)
        try:
            client.sync(mailbox, store)
        except (OSError, ValueError, urllib.error.URLError):
            pass
        finally:
            mailbox.close()
        time.sleep(interval)
