import os
import io
import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from workspace_federation import PeerClient, load_config, prepare_channel, start_host
from workspace_mailbox import Mailbox


class FederationTests(unittest.TestCase):
    def test_peer_token_is_not_forwarded_on_redirect(self):
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/steal-token")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = {"role": "client", "peer_url": f"http://127.0.0.1:{server.server_port}",
                  "token_env": "CODEX_WORKSPACE_REDIRECT_TEST_TOKEN"}
        try:
            with patch.dict(os.environ, {config["token_env"]: "x" * 32}):
                with self.assertRaisesRegex(ValueError, "HTTP 302"):
                    PeerClient(config).request("/overview")
        finally:
            server.shutdown()
            server.server_close()

    def test_peer_url_requires_tailnet_ip_and_explicit_port(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "federation.json"
            config = {"role": "client", "workspace_id": "friend", "peer_id": "owner",
                      "channel_id": "shared:project", "channel_name": "Projet commun",
                      "token_env": "CODEX_WORKSPACE_TEST_URL_TOKEN"}
            with patch.dict(os.environ, {config["token_env"]: "x" * 32}):
                for peer_url in ("http://example.ts.net:8766", "http://100.100.10.20",
                                 "http://100.100.10.20:8766?next=public",
                                 "http://[fd7a:115c:a1e0::1]:8766",
                                 "http://192.168.1.5:8766"):
                    path.write_text(json.dumps({**config, "peer_url": peer_url}))
                    with self.assertRaises(ValueError):
                        load_config(path)
                path.write_text(json.dumps({**config, "peer_url": "http://100.100.10.20:8766"}))
                self.assertEqual(load_config(path)["peer_url"], "http://100.100.10.20:8766")

    def test_tailscale_address_discovery_rejects_non_tailnet_addresses(self):
        from scripts.configure_workspace_federation import tailscale_ipv4
        with patch("scripts.configure_workspace_federation.shutil.which", return_value="tailscale"), \
             patch("scripts.configure_workspace_federation.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, "100.100.10.20\n", "")):
            self.assertEqual(tailscale_ipv4(), "100.100.10.20")
        with patch("scripts.configure_workspace_federation.shutil.which", return_value="tailscale"), \
             patch("scripts.configure_workspace_federation.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, "192.168.1.5\n", "")):
            with self.assertRaises(ValueError):
                tailscale_ipv4()

    def test_private_channel_federation_delivery_and_reconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            host_db = Path(directory) / "host.sqlite"
            client_db = Path(directory) / "client.sqlite"
            token_env = "CODEX_WORKSPACE_TEST_PEER_TOKEN"
            client_token_env = "CODEX_WORKSPACE_TEST_CLIENT_TOKEN"
            previous = os.environ.get(token_env)
            previous_client = os.environ.get(client_token_env)
            os.environ[token_env] = "temporary-test-secret-value-long-enough-1234"
            os.environ[client_token_env] = os.environ[token_env]
            host_config = {"role": "host", "workspace_id": "owner", "peer_id": "friend",
                           "channel_id": "shared:project", "channel_name": "Projet commun",
                           "bind": "127.0.0.1", "port": 0, "token_env": token_env}
            server = start_host(host_config, host_db)
            self.assertEqual(server.last_contact, 0.0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client_config = {"role": "client", "workspace_id": "friend", "peer_id": "owner",
                             "channel_id": "shared:project", "channel_name": "Projet commun",
                             "peer_url": f"http://127.0.0.1:{server.server_port}", "token_env": client_token_env}
            host = Mailbox(host_db)
            friend = Mailbox(client_db)
            try:
                prepare_channel(client_config, friend)
                a = host.register_agent("compte-1", "host-chat", "Alice",
                                        workspace="C:/secret/root/project")["id"]
                b = friend.register_agent("compte-2", "friend-chat", "Bob")["id"]
                host.join_channel(a, "shared:project")
                friend.join_channel(b, "shared:project")
                host.set_presence([host.agent(a)])
                friend.set_presence([friend.agent(b)])
                peer = PeerClient(client_config)
                peer.sync(friend)
                self.assertGreater(server.last_contact, 0.0)
                remote_b = "friend:" + b
                self.assertIn(remote_b, host.shared_members("shared:project"))
                self.assertEqual({item["id"] for item in peer.overview()["agents"]}, {"owner:" + a, b})
                self.assertEqual(next(item for item in peer.overview()["agents"]
                                      if item["id"] == "owner:" + a)["workspace"], "project")
                self.assertIsNone(host.remote_diagnostic(remote_b)["reported_at"])
                status = {"checked_at": "2026-09-25T10:00:00Z", **{key:
                    {"status": "unknown", "detail": "Non vérifié", "at": None}
                    for key in ("github", "github_mcp", "mcp", "hooks", "delivery")}}
                status["mcp"] = {"status": "ok", "detail": "Appel observé", "at": None}
                with patch("workspace_diagnostics.diagnose", return_value=status) as diagnose:
                    peer.sync(friend, object())
                diagnose.assert_called_once()
                reported = host.remote_diagnostic(remote_b)
                self.assertEqual(reported["origin_workspace"], "friend")
                self.assertEqual(reported["mcp"]["status"], "ok")
                private_status = {**status, "github": {**status["github"],
                    "repository": "secret/repository", "checks": {"cli": {"detail": "private"}}}}
                with patch("workspace_federation.urllib.request.urlopen",
                           return_value=io.BytesIO(json.dumps(private_status).encode())):
                    host_diagnostic = peer.diagnostics("owner:" + a)
                self.assertNotIn("repository", host_diagnostic["github"])
                self.assertNotIn("checks", host_diagnostic["github"])
                self.assertNotIn("secret/repository", json.dumps(host_diagnostic))
                self.assertNotIn("private", json.dumps(host_diagnostic))
                from server import merge_federated_overview
                with patch("server.WORKSPACE_MAILBOX_DB", host_db):
                    host_view = merge_federated_overview(host.overview(), host_config, True)
                self.assertTrue(next(item for item in host_view["agents"]
                                     if item["id"] == remote_b)["remote"])

                sent = host.send(a, "Contrat prêt", "shared:project", target_agent=remote_b)
                self.assertEqual([item["content"] for item in peer.inbox(b)], ["Contrat prêt"])
                self.assertEqual(peer.inbox(b, after_id=sent["id"]), [])
                self.assertEqual(len(host.pending(remote_b)), 1)
                from hook_sender import inbox_context
                with patch("hook_sender.load_federation_config", return_value=client_config), \
                     patch("hook_sender.mailbox_path", return_value=client_db):
                    context, local_ids, remote_ids = inbox_context("compte-2", "friend-chat",
                                                                    "UserPromptSubmit")
                self.assertIn("Contrat prêt", context)
                self.assertEqual(local_ids, [])
                self.assertEqual(remote_ids, [sent["id"]])
                peer.messages(agent_id=b)
                self.assertEqual(len(host.pending(remote_b)), 1)
                peer.mark_delivered(b, [sent["id"]])
                self.assertEqual(host.pending(remote_b), [])
                delivered = peer.messages()["messages"][0]["deliveries"]
                self.assertEqual(delivered[0]["agent_id"], b)
                self.assertEqual(delivered[0]["method"], "hook")

                queued = friend.queue_federated(b, "shared:project", "Je vérifie", "owner:" + a,
                                                client_key="retry-safe", references=["PR #42"])
                with patch("server.WORKSPACE_MAILBOX_DB", client_db):
                    merged = merge_federated_overview(friend.overview(), client_config)
                self.assertEqual(merged["federation"]["pending_outbox"][0]["content"], "Je vérifie")
                server.shutdown()
                server.server_close()
                with self.assertRaises((OSError, urllib.error.URLError)):
                    peer.request("/overview")
                self.assertEqual(friend.queued_federated("shared:project")[0]["client_key"], queued["client_key"])

                server = start_host(host_config, host_db)
                client_config["peer_url"] = f"http://127.0.0.1:{server.server_port}"
                peer = PeerClient(client_config)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                peer.sync(friend)
                self.assertEqual(friend.queued_federated("shared:project"), [])
                self.assertEqual([item["content"] for item in host.messages("shared:project")["messages"]],
                                 ["Contrat prêt", "Je vérifie"])
                self.assertEqual(host.messages("shared:project")["messages"][1]["references"], ["PR #42"])
                duplicate = peer.request("/send", {"sender_agent_id": b, "content": "Je vérifie",
                    "target_agent_id": a, "client_key": "retry-safe", "references": ["PR #42"]})["message"]
                self.assertEqual(duplicate["id"], friend.db.execute(
                    "SELECT remote_id FROM federation_outbox WHERE client_key='retry-safe'").fetchone()[0])
                self.assertEqual(len(host.messages("shared:project")["messages"]), 2)
                self.assertEqual([item["content"] for item in peer.messages(pair=(b, "owner:" + a))["messages"]],
                                 ["Contrat prêt", "Je vérifie"])

                private = host.send(a, "Privé local", recipient_agent=host.register_agent(
                    "compte-1", "other-chat")["id"])
                self.assertEqual(len(peer.messages()["messages"]), 2)
                with self.assertRaises(ValueError):
                    peer.message(b, private["id"])
                another = host.send(a, "Lecture MCP", "shared:project", target_agent=remote_b)
                self.assertEqual([item["id"] for item in host.pending(remote_b)], [another["id"]])
                peer.mark_delivered(b, [another["id"]], "mcp")
                self.assertEqual(host.pending(remote_b), [])
                method = host.db.execute("SELECT method FROM message_deliveries WHERE message_id=? "
                    "AND agent_id=?", (another["id"], remote_b)).fetchone()[0]
                self.assertEqual(method, "mcp")
                old_token = os.environ[client_token_env]
                contact_before_denial = server.last_contact
                os.environ[client_token_env] = "wrong-secret-long-enough-1234567890"
                with self.assertRaises(ValueError):
                    peer.overview()
                self.assertEqual(server.last_contact, contact_before_denial)
                os.environ[client_token_env] = old_token
                bulk = [host.send(a, f"Lot {number}", "shared:project",
                                  target_agent=remote_b)["id"] for number in range(51)]
                peer.mark_delivered(b, bulk, "mcp")
                self.assertEqual(host.pending(remote_b), [])
            finally:
                friend.close()
                host.close()
                server.shutdown()
                server.server_close()
                if previous is None:
                    os.environ.pop(token_env, None)
                else:
                    os.environ[token_env] = previous
                if previous_client is None:
                    os.environ.pop(client_token_env, None)
                else:
                    os.environ[client_token_env] = previous_client


if __name__ == "__main__":
    unittest.main()
