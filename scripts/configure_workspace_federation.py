"""Configure one explicitly shared Workspace channel; secrets stay in env vars."""
from __future__ import annotations

import argparse
import ipaddress
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "data" / "workspace-federation.json"


def tailscale_ipv4() -> str:
    executable = shutil.which("tailscale")
    if not executable and sys.platform == "win32":
        standard = Path(r"C:\Program Files\Tailscale\tailscale.exe")
        executable = str(standard) if standard.is_file() else None
    if not executable:
        raise ValueError("Tailscale est introuvable ; installez-le et connectez ce PC")
    try:
        result = subprocess.run([executable, "ip", "-4"], capture_output=True,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Impossible de lire l'adresse Tailscale de ce PC") from exc
    try:
        address = ipaddress.IPv4Address(result.stdout.strip())
    except ipaddress.AddressValueError as exc:
        raise ValueError("Adresse Tailscale absente ; connectez ce PC puis réessayez") from exc
    if result.returncode or address not in ipaddress.ip_network("100.64.0.0/10"):
        raise ValueError("Adresse Tailscale IPv4 invalide ou service déconnecté")
    return str(address)


def main():
    parser = argparse.ArgumentParser(description="Configurer un channel Workspace partagé via Tailscale")
    parser.add_argument("role", choices=("host", "client"))
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--peer-id", required=True)
    parser.add_argument("--channel-id", required=True, help="Exemple : shared:projet")
    parser.add_argument("--channel-name", required=True)
    parser.add_argument("--token-env", default="CODEX_WORKSPACE_PEER_TOKEN")
    parser.add_argument("--bind", help="Adresse IPv4 Tailscale de l'hôte (détectée si omise)")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--dashboard-port", type=int, default=8765)
    parser.add_argument("--peer-url", help="http://adresse-tailscale:8766 sur le client")
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args()
    from workspace_federation import load_config
    config = {"role": args.role, "workspace_id": args.workspace_id, "peer_id": args.peer_id,
              "channel_id": args.channel_id, "channel_name": args.channel_name,
              "token_env": args.token_env}
    if args.role == "host":
        try:
            bind = args.bind or tailscale_ipv4()
        except ValueError as exc:
            parser.error(str(exc))
        config.update({"bind": bind, "port": args.port,
                       "dashboard_port": args.dashboard_port})
    else:
        if not args.peer_url:
            parser.error("--peer-url est requis pour le client")
        config["peer_url"] = args.peer_url
    output = args.config.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        # Validate address and identifiers before replacing a working configuration.
        import os
        missing = args.token_env not in os.environ
        if missing:
            os.environ[args.token_env] = "validation-only-placeholder-123456789012345"
        try:
            load_config(temporary)
        finally:
            if missing:
                del os.environ[args.token_env]
        if output.exists():
            backup = output.with_name(output.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
            shutil.copy2(output, backup)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Configuration enregistrée : {output}")
    print(f"Définissez {args.token_env} sur les deux machines avec le même secret de 32 caractères minimum, "
          "puis redémarrez l'application et Codex. Le secret n'est pas stocké dans ce fichier.")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
