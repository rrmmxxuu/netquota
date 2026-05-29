#!/usr/bin/env python3
import errno
import fcntl
import json
import os
import re
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CONFIG_PATH = os.environ.get("NETQUOTA_CONFIG", "/etc/netquota-agent.conf")
LOCK_PATH = "/run/netquota-agent.lock"
DEFAULT_STATE_FILE = "/var/lib/netquota-agent/state.json"
DEFAULT_TIMEOUT = 10


class ConfigError(Exception):
    pass


@dataclass
class Config:
    worker_url: str
    auth_token: str
    node_id: str
    interfaces: List[str]
    reset_day: int
    reset_hour_utc: int
    total_bytes: int
    state_file: str
    request_timeout: int
    expire_at: int
    billing_mode: str
    include_hostname: bool = True


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_expire_at(value: str) -> int:
    value = (value or "").strip()
    if not value:
        return 0

    if value.isdigit():
        return int(value)

    if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        value = value + "T23:59:59Z"

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError(
            "EXPIRE_AT must be a Unix timestamp, YYYY-MM-DD, or ISO time, "
            "for example 2026-02-23 or 2026-02-23T23:59:59Z"
        ) from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return int(dt.astimezone(timezone.utc).timestamp())


def load_config(path: str = CONFIG_PATH) -> Config:
    raw: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                raw[k.strip()] = v.strip()
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {path}") from exc

    worker_url = raw.get("WORKER_URL", "").rstrip("/")
    auth_token = raw.get("AUTH_TOKEN", "")
    node_id = raw.get("NODE_ID") or socket.gethostname()
    interfaces_value = raw.get("INTERFACES", "auto")
    interfaces = resolve_interfaces(interfaces_value)

    try:
        reset_day = int(raw.get("RESET_DAY", "1"))
        reset_hour = int(raw.get("RESET_HOUR_UTC", "0"))
        total_bytes = int(raw.get("TOTAL_BYTES", "0"))
        timeout = int(raw.get("REQUEST_TIMEOUT", str(DEFAULT_TIMEOUT)))
    except ValueError as exc:
        raise ConfigError(f"invalid numeric config: {exc}") from exc

    state_file = raw.get("STATE_FILE", DEFAULT_STATE_FILE)
    expire_at = parse_expire_at(raw.get("EXPIRE_AT", "0"))
    billing_mode = raw.get("BILLING_MODE", "both").strip().lower()

    if not worker_url:
        raise ConfigError("WORKER_URL is required")
    if not auth_token:
        raise ConfigError("AUTH_TOKEN is required")
    if not (1 <= reset_day <= 31):
        raise ConfigError("RESET_DAY must be between 1 and 31")
    if not (0 <= reset_hour <= 23):
        raise ConfigError("RESET_HOUR_UTC must be between 0 and 23")
    if total_bytes < 0:
        raise ConfigError("TOTAL_BYTES must be >= 0")
    if timeout <= 0:
        raise ConfigError("REQUEST_TIMEOUT must be > 0")
    if expire_at < 0:
        raise ConfigError("EXPIRE_AT must be >= 0")
    if billing_mode not in {"both", "upload", "download"}:
        raise ConfigError("BILLING_MODE must be one of: both, upload, download")
    if not interfaces:
        raise ConfigError("no interfaces resolved from INTERFACES")

    return Config(
        worker_url=worker_url,
        auth_token=auth_token,
        node_id=node_id,
        interfaces=interfaces,
        reset_day=reset_day,
        reset_hour_utc=reset_hour,
        total_bytes=total_bytes,
        state_file=state_file,
        request_timeout=timeout,
        expire_at=expire_at,
        billing_mode=billing_mode,
        include_hostname=parse_bool(raw.get("INCLUDE_HOSTNAME"), True),
    )


def resolve_interfaces(value: str) -> List[str]:
    value = (value or "auto").strip()
    if value.lower() != "auto":
        return [x.strip() for x in value.split(",") if x.strip()]

    detected: List[str] = []
    try:
        with os.popen("ip -o route show default 2>/dev/null") as p:
            for line in p:
                parts = line.strip().split()
                if "dev" in parts:
                    idx = parts.index("dev")
                    if idx + 1 < len(parts):
                        dev = parts[idx + 1]
                        if dev != "lo" and dev not in detected:
                            detected.append(dev)
    except Exception:
        pass

    if detected:
        return detected

    net_dir = Path("/sys/class/net")
    for entry in sorted(net_dir.iterdir()):
        if entry.name == "lo":
            continue
        if (entry / "statistics" / "rx_bytes").exists() and (entry / "statistics" / "tx_bytes").exists():
            detected.append(entry.name)
    return detected


def anchor_for(year: int, month: int, day: int, hour: int) -> datetime:
    actual_day = min(day, monthrange(year, month)[1])
    return datetime(year, month, actual_day, hour, 0, 0, tzinfo=timezone.utc)


def previous_reset(now: datetime, day: int, hour: int) -> datetime:
    this_month = anchor_for(now.year, now.month, day, hour)
    if now >= this_month:
        return this_month
    if now.month == 1:
        return anchor_for(now.year - 1, 12, day, hour)
    return anchor_for(now.year, now.month - 1, day, hour)


def next_reset(now: datetime, day: int, hour: int) -> datetime:
    this_month = anchor_for(now.year, now.month, day, hour)
    if now < this_month:
        return this_month
    if now.month == 12:
        return anchor_for(now.year + 1, 1, day, hour)
    return anchor_for(now.year, now.month + 1, day, hour)


def read_boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()


def read_counters(interfaces: List[str]) -> Tuple[int, int]:
    rx = 0
    tx = 0
    missing = []
    for iface in interfaces:
        base = Path("/sys/class/net") / iface / "statistics"
        rx_path = base / "rx_bytes"
        tx_path = base / "tx_bytes"
        if not rx_path.exists() or not tx_path.exists():
            missing.append(iface)
            continue
        rx += int(rx_path.read_text(encoding="utf-8").strip())
        tx += int(tx_path.read_text(encoding="utf-8").strip())
    if missing and len(missing) == len(interfaces):
        raise RuntimeError(f"all configured interfaces missing: {', '.join(missing)}")
    return rx, tx


def ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def load_state(path: str) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(path: str, state: Dict) -> None:
    ensure_parent(path)
    dir_name = str(Path(path).parent)
    fd, tmp = tempfile.mkstemp(prefix="state.", suffix=".json", dir=dir_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def init_state(cfg: Config, now: datetime) -> Dict:
    rx_raw, tx_raw = read_counters(cfg.interfaces)
    prev = previous_reset(now, cfg.reset_day, cfg.reset_hour_utc)
    nxt = next_reset(now, cfg.reset_day, cfg.reset_hour_utc)
    return {
        "version": 1,
        "node_id": cfg.node_id,
        "interfaces": cfg.interfaces,
        "boot_id": read_boot_id(),
        "last_raw_rx": rx_raw,
        "last_raw_tx": tx_raw,
        "acc_rx": 0,
        "acc_tx": 0,
        "last_sample_ts": iso_z(now),
        "last_upload_ts": None,
        "last_reset_at": int(prev.timestamp()),
        "next_reset_at": int(nxt.timestamp()),
        "reset_day": cfg.reset_day,
        "reset_hour_utc": cfg.reset_hour_utc,
        "total_bytes": cfg.total_bytes,
        "expire_at": cfg.expire_at,
        "last_calibration_ts": None,
    }


def maybe_reset_window(state: Dict, cfg: Config, now: datetime, rx_raw: int, tx_raw: int, boot_id: str) -> bool:
    changed = False
    scheduled_next = int(next_reset(now, cfg.reset_day, cfg.reset_hour_utc).timestamp())
    prev_sched = int(previous_reset(now, cfg.reset_day, cfg.reset_hour_utc).timestamp())

    if state.get("reset_day") != cfg.reset_day or state.get("reset_hour_utc") != cfg.reset_hour_utc:
        state["reset_day"] = cfg.reset_day
        state["reset_hour_utc"] = cfg.reset_hour_utc
        state["next_reset_at"] = scheduled_next
        changed = True

    if now.timestamp() >= state.get("next_reset_at", 0):
        state["acc_rx"] = 0
        state["acc_tx"] = 0
        state["last_reset_at"] = prev_sched
        state["next_reset_at"] = scheduled_next
        state["last_raw_rx"] = rx_raw
        state["last_raw_tx"] = tx_raw
        state["boot_id"] = boot_id
        changed = True
    return changed


def apply_sample(state: Dict, cfg: Config, now: datetime) -> Dict:
    rx_raw, tx_raw = read_counters(cfg.interfaces)
    boot_id = read_boot_id()

    maybe_reset_window(state, cfg, now, rx_raw, tx_raw, boot_id)

    prev_boot = state.get("boot_id")
    prev_rx = int(state.get("last_raw_rx", rx_raw))
    prev_tx = int(state.get("last_raw_tx", tx_raw))

    if prev_boot == boot_id:
        delta_rx = rx_raw - prev_rx if rx_raw >= prev_rx else rx_raw
        delta_tx = tx_raw - prev_tx if tx_raw >= prev_tx else tx_raw
    else:
        delta_rx = rx_raw
        delta_tx = tx_raw

    state["acc_rx"] = int(state.get("acc_rx", 0)) + delta_rx
    state["acc_tx"] = int(state.get("acc_tx", 0)) + delta_tx
    state["last_raw_rx"] = rx_raw
    state["last_raw_tx"] = tx_raw
    state["boot_id"] = boot_id
    state["last_sample_ts"] = iso_z(now)
    state["interfaces"] = cfg.interfaces
    state["node_id"] = cfg.node_id
    state["total_bytes"] = cfg.total_bytes
    state["expire_at"] = cfg.expire_at
    return state


def days_until_reset(now: datetime, cfg: Config) -> int:
    nxt = next_reset(now, cfg.reset_day, cfg.reset_hour_utc)
    seconds_left = max(0, int(nxt.timestamp() - now.timestamp()))
    return (seconds_left + 86399) // 86400


def build_payload(state: Dict, cfg: Config) -> Dict:
    now = utc_now()
    reset_days_left = days_until_reset(now, cfg)

    raw_upload = int(state.get("acc_tx", 0))
    raw_download = int(state.get("acc_rx", 0))

    if cfg.billing_mode == "upload":
        billed_upload = raw_upload
        billed_download = 0
    elif cfg.billing_mode == "download":
        billed_upload = 0
        billed_download = raw_download
    else:
        billed_upload = raw_upload
        billed_download = raw_download

    payload = {
        "node_id": cfg.node_id,
        "upload": int(billed_upload),
        "download": int(billed_download),
        "total": int(cfg.total_bytes),
        "expire": int(cfg.expire_at),
        "reset_day": int(reset_days_left),
        "ts": state.get("last_sample_ts") or iso_z(now),
        "interfaces": cfg.interfaces,
        "billing_mode": cfg.billing_mode,
        "raw_upload": raw_upload,
        "raw_download": raw_download,
    }
    if cfg.include_hostname:
        payload["hostname"] = socket.gethostname()
    return payload


def upload_payload(cfg: Config, payload: Dict) -> None:
    url = cfg.worker_url + "/report"
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {cfg.auth_token}",
            "user-agent": "netquota-agent/1.2",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.request_timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status < 200 or resp.status >= 300:
                raise RuntimeError(f"worker returned HTTP {resp.status}: {body}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"upload failed HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"upload failed: {exc}") from exc


def do_sample(cfg: Config) -> int:
    now = utc_now()
    state = load_state(cfg.state_file)
    if not state:
        state = init_state(cfg, now)
    state = apply_sample(state, cfg, now)
    payload = build_payload(state, cfg)
    upload_payload(cfg, payload)
    state["last_upload_ts"] = iso_z(now)
    save_state(cfg.state_file, state)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def do_reset(cfg: Config) -> int:
    now = utc_now()
    rx_raw, tx_raw = read_counters(cfg.interfaces)
    state = {
        "version": 1,
        "node_id": cfg.node_id,
        "interfaces": cfg.interfaces,
        "boot_id": read_boot_id(),
        "last_raw_rx": rx_raw,
        "last_raw_tx": tx_raw,
        "acc_rx": 0,
        "acc_tx": 0,
        "last_sample_ts": iso_z(now),
        "last_upload_ts": iso_z(now),
        "last_reset_at": int(now.timestamp()),
        "next_reset_at": int(next_reset(now, cfg.reset_day, cfg.reset_hour_utc).timestamp()),
        "reset_day": cfg.reset_day,
        "reset_hour_utc": cfg.reset_hour_utc,
        "total_bytes": cfg.total_bytes,
        "expire_at": cfg.expire_at,
        "last_calibration_ts": None,
    }
    payload = build_payload(state, cfg)
    upload_payload(cfg, payload)
    save_state(cfg.state_file, state)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


SIZE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)$")
UNIT_MULTIPLIERS = {
    "": 1,
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "kib": 1024,
    "m": 1000**2,
    "mb": 1000**2,
    "mib": 1024**2,
    "g": 1000**3,
    "gb": 1000**3,
    "gib": 1024**3,
    "t": 1000**4,
    "tb": 1000**4,
    "tib": 1024**4,
    "p": 1000**5,
    "pb": 1000**5,
    "pib": 1024**5,
}


def parse_size(value: str) -> int:
    m = SIZE_RE.match(value.strip())
    if not m:
        raise ValueError(f"invalid size: {value}")
    num = float(m.group(1))
    unit = m.group(2).lower()
    if unit not in UNIT_MULTIPLIERS:
        raise ValueError(f"unsupported unit in size: {value}")
    return int(num * UNIT_MULTIPLIERS[unit])


def format_bytes(n: int) -> str:
    units = [(1024**4, "TiB"), (1024**3, "GiB"), (1024**2, "MiB"), (1024, "KiB")]
    for factor, suffix in units:
        if n >= factor:
            return f"{n / factor:.3f} {suffix}"
    return f"{n} B"


def parse_calibrate_args(argv: List[str]) -> Tuple[Optional[int], Optional[int]]:
    upload = None
    download = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--upload", "-u"):
            if i + 1 >= len(argv):
                raise ValueError("missing value after --upload")
            upload = parse_size(argv[i + 1])
            i += 2
            continue
        if arg in ("--download", "-d"):
            if i + 1 >= len(argv):
                raise ValueError("missing value after --download")
            download = parse_size(argv[i + 1])
            i += 2
            continue
        raise ValueError(f"unknown calibrate argument: {arg}")

    if upload is None and download is None:
        raise ValueError("calibrate requires at least one of --upload or --download")
    if upload is not None and upload < 0:
        raise ValueError("upload must be >= 0")
    if download is not None and download < 0:
        raise ValueError("download must be >= 0")
    return upload, download


def do_calibrate(cfg: Config, argv: List[str]) -> int:
    target_upload, target_download = parse_calibrate_args(argv)
    now = utc_now()
    rx_raw, tx_raw = read_counters(cfg.interfaces)
    boot_id = read_boot_id()

    state = load_state(cfg.state_file)
    if not state:
        state = init_state(cfg, now)

    maybe_reset_window(state, cfg, now, rx_raw, tx_raw, boot_id)

    before_upload = int(state.get("acc_tx", 0))
    before_download = int(state.get("acc_rx", 0))

    if target_upload is not None:
        state["acc_tx"] = target_upload
    if target_download is not None:
        state["acc_rx"] = target_download

    state["last_raw_rx"] = rx_raw
    state["last_raw_tx"] = tx_raw
    state["boot_id"] = boot_id
    state["last_sample_ts"] = iso_z(now)
    state["last_upload_ts"] = iso_z(now)
    state["last_calibration_ts"] = iso_z(now)
    state["interfaces"] = cfg.interfaces
    state["node_id"] = cfg.node_id
    state["reset_day"] = cfg.reset_day
    state["reset_hour_utc"] = cfg.reset_hour_utc
    state["total_bytes"] = cfg.total_bytes
    state["expire_at"] = cfg.expire_at
    if "next_reset_at" not in state:
        state["next_reset_at"] = int(next_reset(now, cfg.reset_day, cfg.reset_hour_utc).timestamp())
    if "last_reset_at" not in state:
        state["last_reset_at"] = int(previous_reset(now, cfg.reset_day, cfg.reset_hour_utc).timestamp())

    payload = build_payload(state, cfg)
    upload_payload(cfg, payload)
    save_state(cfg.state_file, state)

    result = {
        "ok": True,
        "action": "calibrate",
        "before": {
            "upload": before_upload,
            "download": before_download,
            "upload_human": format_bytes(before_upload),
            "download_human": format_bytes(before_download),
        },
        "after": {
            "upload": int(state.get("acc_tx", 0)),
            "download": int(state.get("acc_rx", 0)),
            "upload_human": format_bytes(int(state.get("acc_tx", 0))),
            "download_human": format_bytes(int(state.get("acc_rx", 0))),
        },
        "payload": payload,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def do_status(cfg: Config) -> int:
    now = utc_now()
    state = load_state(cfg.state_file)
    if not state:
        state = init_state(cfg, now)
        save_state(cfg.state_file, state)
    state["expire_at"] = cfg.expire_at
    state["total_bytes"] = cfg.total_bytes
    state["reset_day"] = cfg.reset_day
    state["reset_hour_utc"] = cfg.reset_hour_utc
    payload = build_payload(state, cfg)
    print(json.dumps({"state": state, "payload": payload}, ensure_ascii=False, indent=2))
    return 0


def acquire_lock() -> int:
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise RuntimeError("another netquota-agent process is running") from exc
        raise
    return fd


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print("usage: netquota-agent.py [sample|reset|status|calibrate]", file=sys.stderr)
        return 2

    cmd = argv[1]
    try:
        cfg = load_config()
        lock_fd = acquire_lock()
        try:
            if cmd == "sample":
                return do_sample(cfg)
            if cmd == "reset":
                return do_reset(cfg)
            if cmd == "status":
                return do_status(cfg)
            if cmd == "calibrate":
                return do_calibrate(cfg, argv[2:])
            print(f"unknown command: {cmd}", file=sys.stderr)
            return 2
        finally:
            os.close(lock_fd)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
