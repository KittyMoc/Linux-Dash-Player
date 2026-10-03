#!/usr/bin/env python3
"""
Linux Dash Agent — publica las métricas reales de esta PC y la enlaza con tu página.

Cómo encaja todo:
    [ esta PC ]  --(127.0.0.1:8000)-->  Tailscale Serve  --(HTTPS, solo tu tailnet)-->  [ tu página / app ]
    El agente NUNCA escucha en la red: solo en localhost. Tailscale lo publica con
    HTTPS dentro de tu red privada, y cada petición exige un token secreto.

Uso (todo se hace una sola vez, salvo "pair" cuando quieras vincular otro dispositivo):
    python3 linux_dash_agent.py setup            # publica el agente en tu tailnet (tailscale serve)
    python3 linux_dash_agent.py install-service  # opcional: que arranque solo con tu sesión
    python3 linux_dash_agent.py pair             # imprime el enlace (y un QR) para vincular la página

Otros comandos:
    run             arranca el agente en primer plano (lo que usa el servicio)
    pair --rotate   genera un token nuevo (invalida los enlaces anteriores)
    pair --base URL usa esta dirección en vez de detectar la de Tailscale

Dependencias: flask psutil   (opcional: segno -> QR en la terminal, waitress -> servidor más robusto)
Requiere Python >= 3.10.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "linux-dash"
CONFIG_FILE = CONFIG_DIR / "agent.json"
SERVICE_FILE = (Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
                / "systemd" / "user" / "linux-dash-agent.service")

DEFAULT_PAGE = "https://kittymoc.github.io/Linux-Dash-Player/"
DEFAULT_PORT = 8000


# --------------------------------------------------------------------------- #
# Configuración (token secreto guardado con permisos 0600)
# --------------------------------------------------------------------------- #
def load_config(rotate: bool = False) -> dict:
    cfg: dict = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
        except (OSError, ValueError):
            cfg = {}
    changed = False
    if rotate or not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(24)
        changed = True
    for key, default in (("page", DEFAULT_PAGE), ("port", DEFAULT_PORT)):
        if key not in cfg:
            cfg[key] = default
            changed = True
    if changed:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(cfg, fh, indent=2)
        os.chmod(CONFIG_FILE, 0o600)
    return cfg


def origin_of(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}"


# --------------------------------------------------------------------------- #
# Tailscale
# --------------------------------------------------------------------------- #
def tailscale_base() -> tuple[str | None, str | None]:
    """Devuelve (url_https_de_esta_pc, aviso). La URL es None si no se puede detectar."""
    exe = shutil.which("tailscale")
    if not exe:
        return None, "No encuentro el comando 'tailscale'. ¿Está instalado y con sesión iniciada?"
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True,
                             text=True, timeout=10, check=True).stdout
        data = json.loads(out)
    except (subprocess.SubprocessError, ValueError, OSError) as exc:
        return None, f"'tailscale status' falló: {exc}"
    dns = (data.get("Self") or {}).get("DNSName", "").rstrip(".")
    if not dns:
        return None, "Tailscale no devolvió el nombre de esta PC (¿MagicDNS desactivado?)."
    warning = None
    if not data.get("CertDomains"):
        warning = ("Parece que HTTPS no está activado en tu tailnet. Actívalo en "
                   "https://login.tailscale.com/admin/dns (sección 'HTTPS Certificates').")
    return f"https://{dns}", warning


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #
def cmd_run(args) -> int:
    cfg = load_config()
    origins = [origin_of(cfg["page"]), *args.allow_origin]
    os.environ["LINUX_DASH_TOKEN"] = cfg["token"]
    os.environ["LINUX_DASH_ALLOWED_ORIGINS"] = ",".join(origins)
    sys.path.insert(0, str(HERE))
    import linux_dash_api as api  # se importa DESPUÉS de fijar las variables

    host, port = "127.0.0.1", int(cfg["port"])
    print(f"Linux Dash Agent en http://{host}:{port}  (solo local)")
    print(f"Páginas autorizadas: {', '.join(origins)}")
    print("Para vincular la página, en otra terminal:  python3 linux_dash_agent.py pair")
    try:
        from waitress import serve
        serve(api.app, host=host, port=port, threads=8)
    except ImportError:
        api.app.run(host=host, port=port, threaded=True)
    return 0


def cmd_pair(args) -> int:
    cfg = load_config(rotate=args.rotate)
    if args.rotate:
        print("Token nuevo generado: los enlaces anteriores dejan de funcionar.\n")
    base, warning = (args.base.rstrip("/"), None) if args.base else tailscale_base()
    if not base:
        print(f"✗ {warning}\n  Si ya tienes la dirección, usa:  pair --base https://mi-pc.tu-red.ts.net")
        return 1
    if warning:
        print(f"⚠ {warning}\n")

    payload = json.dumps({"u": base, "t": cfg["token"]}, separators=(",", ":"))
    code = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    link = f"{cfg['page'].rstrip('/')}/#pair={code}"

    print("Abre este enlace en el dispositivo que quieras vincular (¡es secreto, como una contraseña!):\n")
    print(link, "\n")
    try:
        import segno
        segno.make(link, error="l").terminal(compact=True)
    except ImportError:
        print("(Para ver un QR aquí:  pip install segno)")
    return 0


def _tailscale(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tailscale", *argv], capture_output=True, text=True, timeout=30)


def cmd_setup(args) -> int:
    cfg = load_config()
    if not shutil.which("tailscale"):
        print("✗ Instala Tailscale primero: https://tailscale.com/download  (y ejecuta 'sudo tailscale up').")
        return 1
    port = str(cfg["port"])
    # Sintaxis nueva (>= 1.52) y, si falla, la antigua.
    attempts = [("serve", "--bg", port), ("serve", "https", "/", f"http://127.0.0.1:{port}")]
    last = None
    for argv in attempts:
        last = _tailscale(*argv)
        if last.returncode == 0:
            print(last.stdout.strip() or "Listo.")
            print("\n✓ Agente publicado dentro de tu tailnet con HTTPS. Siguiente paso:  python3 linux_dash_agent.py pair")
            return 0
    print("✗ 'tailscale serve' no funcionó:\n" + ((last.stderr or last.stdout).strip() if last else ""))
    print("\nPosibles causas:\n"
          "  • HTTPS no está activado en tu tailnet: https://login.tailscale.com/admin/dns\n"
          "  • Falta permiso. Una sola vez:  sudo tailscale set --operator=$USER")
    return 1


def cmd_install_service(args) -> int:
    SERVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SERVICE_FILE.write_text(
        "[Unit]\n"
        "Description=Linux Dash Agent\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={sys.executable} {Path(__file__).resolve()} run\n"
        "Restart=on-failure\n"
        "RestartSec=3\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    for argv in (["daemon-reload"], ["enable", "--now", SERVICE_FILE.name]):
        r = subprocess.run(["systemctl", "--user", *argv], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"✗ systemctl --user {' '.join(argv)}: {r.stderr.strip()}")
            return 1
    print(f"✓ Servicio instalado y en marcha ({SERVICE_FILE}).")
    print("  Ver estado:   systemctl --user status linux-dash-agent")
    print("  Que corra aunque no tengas sesión abierta:   loginctl enable-linger $USER")
    print(f"  Quitarlo:     systemctl --user disable --now linux-dash-agent && rm {SERVICE_FILE}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Linux Dash Agent")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("run", help="arranca el agente")
    p.add_argument("--allow-origin", action="append", default=[], metavar="URL",
                   help="autoriza otro origen web (útil para pruebas locales)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("pair", help="imprime el enlace/QR para vincular la página")
    p.add_argument("--rotate", action="store_true", help="genera un token nuevo")
    p.add_argument("--base", help="dirección pública del agente (si no usas la autodetección)")
    p.set_defaults(fn=cmd_pair)

    sub.add_parser("setup", help="publica el agente con tailscale serve").set_defaults(fn=cmd_setup)
    sub.add_parser("install-service", help="arranque automático (systemd --user)").set_defaults(fn=cmd_install_service)

    args = ap.parse_args()
    if not args.cmd:
        args = ap.parse_args(["run"])
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
