#!/usr/bin/env python3
"""
linux-dash API — reescritura moderna en Python (Flask + psutil).

Reemplaza a linux_json_api.sh. Cada "módulo" del script original es ahora una
función que devuelve datos Python; Flask se encarga de serializarlos a JSON.

Uso:
    pip install flask psutil
    python3 linux_dash_api.py

    GET /api/                    -> lista de módulos
    GET /api/<modulo>            -> JSON del módulo, p. ej. /api/current_ram
    GET /server/?module=<modulo> -> ruta que usa el frontend original
    GET /                        -> interfaz web (index.html, .js y .css de app/)

Variables de entorno (todas opcionales):
    LINUX_DASH_HOST         interfaz de escucha           (por defecto 127.0.0.1)
    LINUX_DASH_PORT         puerto                        (por defecto 8000)
    LINUX_DASH_STATIC       carpeta del frontend          (por defecto: carpeta padre del script)
    LINUX_DASH_TOKEN        si se define, /server/ y /api/ exigen "Authorization: Bearer <token>"
    LINUX_DASH_ALLOWED_ORIGINS  orígenes web autorizados (CORS), separados por comas;
                            p. ej. https://usuario.github.io (lo configura el agente)
    LINUX_DASH_PING_HOSTS   hosts para el módulo ping, separados por comas
    LINUX_DASH_EXTERNAL_IP  "1" para consultar la IP pública (hace una petición
                            HTTPS a api.ipify.org; desactivado por defecto)
    LINUX_DASH_REDIS_ARGS   argumentos extra para redis-cli (p. ej. "-a clave")

Requiere Python >= 3.10.
"""
from __future__ import annotations

import hmac
import json
import os
import platform
import pwd
import re
import shlex
import socket
import subprocess
import threading
import time
import urllib.request
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import psutil
from flask import Flask, abort, jsonify, request, send_from_directory

app = Flask(__name__)
app.json.sort_keys = False  # respeta el orden en que definimos las claves

MODULES: dict[str, callable] = {}


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def module(fn):
    """Registra la función como módulo accesible por la API."""
    MODULES[fn.__name__] = fn
    return fn


def run(cmd: list[str], timeout: float = 5) -> str | None:
    """Ejecuta un comando SIN shell. Devuelve stdout, o None si falla."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def human(n: float) -> str:
    """Bytes -> texto legible (1.5G)."""
    n /= 1024  # empezamos en K: el frontend de disco no entiende "B"
    for unit in ("K", "M", "G", "T"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}P"


def human_duration(seconds: int) -> str:
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d} days")
    if h:
        parts.append(f"{h} hours")
    if m:
        parts.append(f"{m} minutes")
    parts.append(f"{s} seconds")
    return " ".join(parts[:-1] + (["and"] if len(parts) > 1 else []) + parts[-1:])


_sample_lock = threading.Lock()
_sample_cache: dict[str, tuple[float, object, object]] = {}
MIN_SAMPLE_DT = 0.2  # s: por debajo de esto se reutiliza la última medición


def _rate_since_last_call(key: str, read, rate):
    """
    Mide "desde la última llamada" en vez de dormir 1 s dentro de la petición.
    Así el frontend puede consultar más seguido (zoom) sin bloquear hilos y sin
    que dos peticiones simultáneas se pisen: si la anterior fue hace menos de
    MIN_SAMPLE_DT se devuelve el último resultado.
    `read()` devuelve el estado crudo; `rate(prev, cur, dt)` calcula el valor.
    """
    with _sample_lock:
        prev = _sample_cache.get(key)
    if prev is None:  # primera vez: una medición corta para arrancar
        first = read()
        time.sleep(0.25)
        prev = (time.monotonic() - 0.25, first, None)
        with _sample_lock:
            _sample_cache[key] = prev
    now, cur = time.monotonic(), None
    t0, raw0, last = prev
    if last is not None and now - t0 < MIN_SAMPLE_DT:
        return last
    cur = read()
    value = rate(raw0, cur, max(now - t0, 1e-3))
    with _sample_lock:
        _sample_cache[key] = (now, cur, value)
    return value


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Sistema
# --------------------------------------------------------------------------- #
@module
def general_info():
    try:
        os_name = platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
    except OSError:
        os_name = platform.system()
    uptime = int(time.time() - psutil.boot_time())
    return {
        "OS": f"{os_name} {platform.release()}",
        "Hostname": socket.gethostname(),
        "Uptime": human_duration(uptime),
        "Server Time": datetime.now().astimezone().strftime("%c %Z"),
    }


@module
def cpu_info():
    out = run(["lscpu", "-J"])
    if out:
        try:
            rows = json.loads(out)["lscpu"]
            return {r["field"].rstrip(":"): r["data"] for r in rows}
        except (KeyError, ValueError):
            pass
    freq = psutil.cpu_freq()
    return {
        "Architecture": platform.machine(),
        "CPU(s)": psutil.cpu_count(logical=True),
        "Core(s)": psutil.cpu_count(logical=False),
        "Model name": platform.processor() or "unknown",
        "CPU MHz": round(freq.current, 2) if freq else None,
    }


@module
def number_of_cpu_cores():
    return {
        "logical": psutil.cpu_count(logical=True),
        "physical": psutil.cpu_count(logical=False),
    }


@module
def cpu_utilization():
    """Uso total de CPU en % desde la última consulta (sin bloquear).
    El frontend espera un número."""
    return _rate_since_last_call(
        "cpu",
        lambda: psutil.cpu_times(),
        lambda a, b, dt: round(_cpu_busy_pct(a, b), 1),
    )


def _cpu_busy_pct(a, b) -> float:
    total = sum(b) - sum(a)
    idle = (b.idle + getattr(b, "iowait", 0)) - (a.idle + getattr(a, "iowait", 0))
    return 0.0 if total <= 0 else max(0.0, min(100.0, 100.0 * (total - idle) / total))


@module
def cpu_utilization_per_core():
    """Extra (no lo usa el frontend original): uso por núcleo."""
    return psutil.cpu_percent(interval=1, percpu=True)


def _temperatures() -> dict:
    if not hasattr(psutil, "sensors_temperatures"):
        return {"current": None, "sensors": {}}
    sensors = psutil.sensors_temperatures() or {}
    data = {
        chip: [
            {"label": t.label or chip, "current": t.current,
             "high": t.high, "critical": t.critical}
            for t in temps
        ]
        for chip, temps in sensors.items()
    }
    current = None
    for chip in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
        if data.get(chip):
            current = data[chip][0]["current"]
            break
    return {"current": current, "sensors": data}


@module
def cpu_temp():
    """Temperatura de CPU en °C como número (0 si no hay sensor)."""
    current = _temperatures()["current"]
    return round(current) if current is not None else 0


@module
def cpu_temp_sensors():
    """Extra: todos los sensores de temperatura con sus umbrales."""
    return _temperatures()


@module
def load_avg():
    cores = psutil.cpu_count(logical=True) or 1
    one, five, fifteen = os.getloadavg()
    return {
        "1_min_avg": round(one * 100 / cores, 2),
        "5_min_avg": round(five * 100 / cores, 2),
        "15_min_avg": round(fifteen * 100 / cores, 2),
    }


# --------------------------------------------------------------------------- #
# Memoria
# --------------------------------------------------------------------------- #
@module
def current_ram():
    """Valores en MiB. 'available' es la estimación real del kernel."""
    vm = psutil.virtual_memory()
    mib = 1024 * 1024
    return {
        "total": round(vm.total / mib, 2),
        "used": round((vm.total - vm.available) / mib, 2),
        "available": round(vm.available / mib, 2),
    }


@module
def memory_info():
    """Contenido completo de /proc/meminfo (valores enteros en kB)."""
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        info[key] = int(rest.split()[0])
    return info


@module
def swap():
    lines = Path("/proc/swaps").read_text().splitlines()[1:]
    result = []
    for line in lines:
        f = line.split()
        if len(f) >= 5:
            result.append({"filename": f[0], "type": f[1], "size": f[2],
                           "used": f[3], "priority": f[4]})
    return result


# --------------------------------------------------------------------------- #
# Procesos
# --------------------------------------------------------------------------- #
def _top_processes(sort_key: str, sample_cpu: bool, limit: int = 15):
    procs = list(psutil.process_iter())
    if sample_cpu:
        for p in procs:  # primera lectura: fija la referencia
            try:
                p.cpu_percent(None)
            except psutil.Error:
                pass
        time.sleep(0.5)

    rows = []
    for p in procs:
        try:
            with p.oneshot():
                mem = p.memory_info()
                rows.append({
                    "pid": p.pid,
                    "user": p.username(),
                    "cpu%": round(p.cpu_percent(None), 1),
                    "mem%": round(p.memory_percent(), 1),
                    "rss": mem.rss // 1024,   # kB, igual que ps
                    "vsz": mem.vms // 1024,   # kB
                    "cmd": p.name(),
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    rows.sort(key=lambda r: (r[sort_key], r["rss"]), reverse=True)
    return rows[:limit]


@module
def cpu_intensive_processes():
    return _top_processes("cpu%", sample_cpu=True)


@module
def ram_intensive_processes():
    return _top_processes("mem%", sample_cpu=False)


@module
def docker_processes():
    """Uso de recursos por contenedor (docker stats, sin depender de 'top')."""
    out = run(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=15)
    if not out:
        return []
    rows = []
    for line in out.splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        rows.append({
            "cname": c.get("Name"),
            "id": c.get("ID"),
            "cpu%": c.get("CPUPerc"),
            "mem%": c.get("MemPerc"),
            "mem_usage": c.get("MemUsage"),
            "pids": c.get("PIDs"),
        })
    return rows


@module
def pm2_stats():
    out = run(["pm2", "jlist"], timeout=10)
    if not out:
        return []
    try:
        apps = json.loads(out[out.index("["):])
    except ValueError:
        return []
    return [{
        "appName": a.get("name"),
        "id": a.get("pm_id"),
        "mode": a.get("pm2_env", {}).get("exec_mode"),
        "pid": a.get("pid"),
        "status": a.get("pm2_env", {}).get("status"),
        "restart": a.get("pm2_env", {}).get("restart_time"),
        "uptime": a.get("pm2_env", {}).get("pm_uptime"),
        "memory": a.get("monit", {}).get("memory"),
        "cpu": a.get("monit", {}).get("cpu"),
    } for a in apps]


# --------------------------------------------------------------------------- #
# Disco
# --------------------------------------------------------------------------- #
@module
def disk_partitions():
    rows = []
    for part in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        rows.append({
            "file_system": part.device,
            "type": part.fstype,
            "size": human(u.total),
            "used": human(u.used),
            "avail": human(u.free),
            "used%": f"{u.percent:.0f}%",
            "mounted": part.mountpoint,
            "size_bytes": u.total,
            "used_bytes": u.used,
        })
    return rows


@module
def io_stats():
    counters = psutil.disk_io_counters(perdisk=True) or {}
    return [{
        "device": dev,
        "reads": c.read_count,
        "writes": c.write_count,
        "read_bytes": c.read_bytes,
        "write_bytes": c.write_bytes,
        "time": getattr(c, "busy_time", None),  # ms con E/S activa (Linux)
    } for dev, c in counters.items() if c.read_count or c.write_count]


# --------------------------------------------------------------------------- #
# Red
# --------------------------------------------------------------------------- #
@module
def bandwidth():
    """Bytes acumulados por interfaz desde el arranque."""
    return [{"interface": nic, "tx": c.bytes_sent, "rx": c.bytes_recv}
            for nic, c in psutil.net_io_counters(pernic=True).items()]


def _net_rate(direction: str) -> dict:
    """KB/s por interfaz desde la última consulta (sin dormir dentro de la petición)."""
    attr = "bytes_recv" if direction == "down" else "bytes_sent"

    def rate(a, b, dt):
        return {n: round((getattr(b[n], attr) - getattr(a[n], attr)) / 1024 / dt)
                for n in b if n in a}

    return _rate_since_last_call(
        f"net-{direction}", lambda: psutil.net_io_counters(pernic=True), rate)


@module
def download_transfer_rate():
    return _net_rate("down")


@module
def upload_transfer_rate():
    return _net_rate("up")


@module
def ip_addresses():
    rows = []
    for nic, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family in (socket.AF_INET, socket.AF_INET6):
                rows.append({
                    "interface": nic,
                    "ip": a.address.split("%")[0],
                    "family": "ipv4" if a.family == socket.AF_INET else "ipv6",
                })
    if os.environ.get("LINUX_DASH_EXTERNAL_IP") == "1":
        try:
            with urllib.request.urlopen("https://api.ipify.org", timeout=3) as r:
                rows.append({"interface": "external", "ip": r.read().decode().strip(),
                             "family": "ipv4"})
        except OSError:
            rows.append({"interface": "external", "ip": None, "family": None})
    return rows


@module
def network_connections():
    """Conexiones inet agrupadas por dirección remota."""
    try:
        conns = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        return []
    counter = Counter(f"{c.raddr.ip}:{c.raddr.port}" for c in conns if c.raddr)
    return [{"connections": n, "address": addr} for addr, n in counter.most_common()]


@module
def arp_cache():
    lines = Path("/proc/net/arp").read_text().splitlines()[1:]
    rows = []
    for line in lines:
        f = line.split()
        if len(f) >= 6:  # IP, HW type, Flags, HW addr, Mask, Device
            rows.append({"addr": f[0], "hw_type": f[1], "hw_addr": f[3],
                         "mask": f[4], "device": f[5]})
    return rows


_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-:]*[A-Za-z0-9])?$")
_RTT_RE = re.compile(r"=\s*[\d.]+/([\d.]+)/")


def _ping_one(host: str) -> dict:
    out = run(["ping", "-c", "2", "-W", "2", "-q", host], timeout=10)
    m = _RTT_RE.search(out or "")
    return {"host": host, "ping": float(m.group(1)) if m else None}


@module
def ping():
    raw = os.environ.get("LINUX_DASH_PING_HOSTS", "1.1.1.1,8.8.8.8")
    hosts = [h.strip() for h in raw.split(",") if _HOST_RE.match(h.strip())]
    with ThreadPoolExecutor(max_workers=max(len(hosts), 1)) as pool:
        return list(pool.map(_ping_one, hosts))


# --------------------------------------------------------------------------- #
# Usuarios y sesiones
# --------------------------------------------------------------------------- #
def _uid_min() -> int:
    try:
        for line in Path("/etc/login.defs").read_text().splitlines():
            if line.startswith("UID_MIN"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 1000


@module
def user_accounts():
    uid_min = _uid_min()
    return [{
        "type": "user" if uid_min <= u.pw_uid < 65534 else "system",
        "user": u.pw_name,
        "home": u.pw_dir,
    } for u in pwd.getpwall()]


@module
def logged_in_users():
    return [{"user": u.name, "from": u.host or "-", "when": iso(u.started),
             "terminal": u.terminal} for u in psutil.users()]


@module
def recent_account_logins():
    """Últimos inicios de sesión (wtmp). Nota: en sistemas sin wtmp devuelve []."""
    out = run(["last", "-n", "20", "-w", "--time-format", "iso"])
    if not out:
        return []
    rows = []
    for line in out.splitlines():
        f = line.split()
        if len(f) < 3 or f[0] in ("wtmp", "btmp"):
            continue
        has_host = not re.match(r"^\d{4}-\d{2}-", f[2])
        host = f[2] if has_host else ""
        date = f[3] if has_host and len(f) > 3 else f[2]
        rows.append({"user": f[0], "tty": f[1], "ip": host, "date": date})
    return rows


# --------------------------------------------------------------------------- #
# Cron
# --------------------------------------------------------------------------- #
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _parse_cron(text: str, user: str | None) -> list[dict]:
    """`user` fijo para crontabs de usuario; None = el archivo trae columna usuario."""
    jobs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or _ENV_RE.match(line):
            continue
        f = line.split()
        if f[0].startswith("@"):
            time_fields, rest = [f[0], "", "", "", ""], f[1:]
        else:
            time_fields, rest = f[:5], f[5:]
        if user is None:
            if not rest:
                continue
            job_user, rest = rest[0], rest[1:]
        else:
            job_user = user
        if len(time_fields) < 5 or not rest:
            continue
        jobs.append(dict(zip(("min", "hrs", "day", "month", "wkday"), time_fields))
                    | {"user": job_user, "CMD": " ".join(rest)})
    return jobs


@module
def scheduled_crons():
    jobs: list[dict] = []

    def read(path: Path) -> str:
        try:
            return path.read_text()
        except (OSError, UnicodeDecodeError):
            return ""

    jobs += _parse_cron(read(Path("/etc/crontab")), None)
    cron_d = Path("/etc/cron.d")
    if cron_d.is_dir():
        for f in sorted(cron_d.iterdir()):
            if f.is_file():
                jobs += _parse_cron(read(f), None)
    # Crontabs de usuario (Debian/Ubuntu vs. RHEL/Fedora/Arch); requiere root
    for spool in (Path("/var/spool/cron/crontabs"), Path("/var/spool/cron")):
        if spool.is_dir():
            for f in sorted(spool.iterdir()):
                if f.is_file():
                    jobs += _parse_cron(read(f), f.name)
            break
    return jobs


@module
def cron_history():
    """Últimas ejecuciones de cron: journald primero, archivos de log como respaldo."""
    out = run(["journalctl", "-t", "CRON", "-t", "crond", "-t", "cron",
               "-n", "50", "-o", "json", "--no-pager"])
    rows = []
    if out:
        for line in out.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            msg = e.get("MESSAGE", "")
            m = re.match(r"\((\S+)\)", msg)
            ts = int(e.get("__REALTIME_TIMESTAMP", 0)) / 1_000_000
            rows.append({"time": iso(ts) if ts else "",
                         "user": m.group(1) if m else "",
                         "message": f'{e.get("SYSLOG_IDENTIFIER", "")} {msg}'.strip()})
        if rows:
            return rows

    for path in ("/var/log/syslog", "/var/log/cron", "/var/log/messages"):
        try:
            with open(path, errors="replace") as fh:
                lines = deque((l for l in fh if "CRON" in l or "crond" in l), maxlen=50)
        except OSError:
            continue
        for l in lines:
            parts = l.split()
            when = parts[0] if parts and "T" in parts[0] else " ".join(parts[:3])
            m = re.search(r"\((\S+)\)", l)
            rows.append({"time": when, "user": m.group(1) if m else "",
                         "message": l.strip()})
        return rows
    return []


# --------------------------------------------------------------------------- #
# Aplicaciones y servicios
# --------------------------------------------------------------------------- #
_APPS = ["php", "node", "mysql", "mongod", "vim", "python3", "ruby", "java",
         "apache2", "httpd", "nginx", "openssl", "vsftpd", "make", "docker",
         "git", "psql", "redis-server"]


@module
def common_applications():
    import shutil
    rows = []
    for name in _APPS:
        path = shutil.which(name)
        rows.append({"binary": name, "location": path or "", "installed": bool(path)})
    return rows


@module
def memcached():
    try:
        with socket.create_connection(("127.0.0.1", 11211), timeout=1) as s:
            s.sendall(b"stats\r\n")
            s.settimeout(1)
            buf = b""
            while not buf.endswith(b"END\r\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
    except OSError:
        return {}
    stats = {}
    for line in buf.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "STAT" and "bytes" in parts[1]:
            stats[parts[1]] = int(parts[2]) if parts[2].isdigit() else parts[2]
    return stats


@module
def redis():
    cmd = ["redis-cli", *shlex.split(os.environ.get("LINUX_DASH_REDIS_ARGS", "")), "INFO"]
    out = run(cmd)
    if not out:
        return {}
    wanted = ("redis_version", "connected_clients", "connected_slaves",
              "used_memory_human", "total_connections_received",
              "total_commands_processed")
    info = {}
    for line in out.splitlines():
        key, _, val = line.strip().partition(":")
        if key in wanted:
            info[key] = val
    return info


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _allowed_origins() -> set[str]:
    raw = os.environ.get("LINUX_DASH_ALLOWED_ORIGINS", "")
    return {o.strip().rstrip("/") for o in raw.split(",") if o.strip()}


@app.before_request
def check_token():
    if request.method == "OPTIONS":
        return None  # el preflight de CORS nunca lleva credenciales
    token = os.environ.get("LINUX_DASH_TOKEN")
    if not token:
        return None
    # Con token configurado se protegen los datos; los archivos estáticos son públicos.
    if not request.path.startswith(("/server/", "/api/")):
        return None
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied, token):
        return jsonify(success=False, status="Unauthorized"), 401
    return None


@app.after_request
def add_headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    origin = request.headers.get("Origin", "").rstrip("/")
    if origin and origin in _allowed_origins():
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        # Chrome exige esto cuando una web pública consulta una dirección privada (Tailscale)
        resp.headers["Access-Control-Allow-Private-Network"] = "true"
        resp.headers["Access-Control-Max-Age"] = "600"
    return resp


def _dispatch(name: str | None):
    fn = MODULES.get(name or "")
    if fn is None:
        return jsonify(success=False, status="Invalid module"), 404
    try:
        return jsonify(fn())
    except Exception:  # noqa: BLE001 — no filtrar detalles internos al cliente
        app.logger.exception("Error en el módulo %s", name)
        return jsonify(success=False, status="Module failed"), 500


# Frontend estático de linux-dash (carpeta app/). Por defecto: la carpeta padre de
# este archivo, es decir app/ si el script vive en app/server/.
STATIC_DIR = Path(os.environ.get("LINUX_DASH_STATIC",
                                 Path(__file__).resolve().parent.parent))
STATIC_FILES = {"index.html", "linuxDash.min.js", "linuxDash.min.css"}
# También se sirven variantes del tema: index.nico.html, index.wmp.html, ...
THEME_RE = re.compile(r"^index\.[A-Za-z0-9_-]+\.html$")


@app.get("/")
def index_page():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/<path:filename>")
def static_file(filename):
    if filename not in STATIC_FILES and not THEME_RE.match(filename):
        abort(404)  # nunca exponemos server/ ni otros archivos
    return send_from_directory(STATIC_DIR, filename)


@app.get("/api/")
def list_modules():
    return jsonify(sorted(MODULES))


@app.get("/api/<name>")
def api_module(name):
    return _dispatch(name)


@app.get("/server/")
def legacy_module():
    return _dispatch(request.args.get("module"))


if __name__ == "__main__":
    app.run(
        host=os.environ.get("LINUX_DASH_HOST", "127.0.0.1"),
        port=int(os.environ.get("LINUX_DASH_PORT", "8000")),
        threaded=True,
    )
