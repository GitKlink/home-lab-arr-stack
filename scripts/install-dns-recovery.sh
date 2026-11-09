#!/usr/bin/env bash
# Install root-owned snapshots; Git checkout changes do not change boot code.
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "Usage: sudo bash scripts/install-dns-recovery.sh <NAS_IP> <STACK_DIR>" >&2
    exit 1
fi
if [[ $EUID -ne 0 ]]; then
    echo "Run this installer with sudo bash." >&2
    exit 1
fi
for tool in python3 docker ip install systemctl mktemp; do
    command -v "$tool" >/dev/null || { echo "Missing command: $tool" >&2; exit 1; }
done
[[ -x /usr/bin/python3 ]] || { echo "Missing /usr/bin/python3" >&2; exit 1; }

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
helper=/usr/local/sbin/arr-dns-recover
unit=/etc/systemd/system/arr-dns-recover.service
config=/etc/arr-stack/dns-recovery.ini
unit_name=arr-dns-recover.service
for target in "$helper" "$unit" "$config"; do
    if [[ -e $target || -L $target ]]; then
        echo "Existing installation at $target; inspect and uninstall before replacing." >&2
        exit 1
    fi
done
if systemctl cat "$unit_name" >/dev/null 2>&1; then
    echo "A recovery unit is already registered; inspect it before replacing." >&2
    exit 1
fi
systemctl cat docker_serv.service >/dev/null

stage=$(mktemp -d)
installed=()
committed=0
cleanup() {
    local status=$?
    if [[ $committed -eq 0 && ${#installed[@]} -gt 0 ]]; then
        systemctl disable "$unit_name" >/dev/null 2>&1 || true
        rm -f -- "${installed[@]}"
        systemctl daemon-reload || true
        echo "Installation failed; newly installed files were removed." >&2
    fi
    rm -rf -- "$stage"
    return "$status"
}
trap cleanup EXIT

install -m 0750 "$script_dir/arr-dns-recover.py" "$stage/helper"
install -m 0644 "$script_dir/../systemd/arr-dns-recover.service" "$stage/unit"
/usr/bin/python3 - "$1" "$2" "$stage/config.ini" <<'PY'
import configparser
import ipaddress
from pathlib import Path
import sys

address = str(ipaddress.IPv4Address(sys.argv[1]))
directory = Path(sys.argv[2])
if not directory.is_absolute() or any(c in str(directory) for c in "\r\n"):
    raise SystemExit("STACK_DIR must be an absolute path without newlines")
config = configparser.ConfigParser(interpolation=None)
config["dns-recovery"] = {"nas_ip": address, "compose_path": str(directory / "docker-compose.arr-stack.yml")}
with open(sys.argv[3], "w", encoding="utf-8") as stream:
    config.write(stream)
PY

# This must succeed without starting, reconnecting or restarting containers.
/usr/bin/python3 "$stage/helper" --config "$stage/config.ini" --check

if [[ ! -d /etc/arr-stack ]]; then
    install -o root -g root -m 0755 -d /etc/arr-stack
fi
installed+=("$helper")
install -o root -g root -m 0750 "$stage/helper" "$helper"
installed+=("$config")
install -o root -g root -m 0600 "$stage/config.ini" "$config"
installed+=("$unit")
install -o root -g root -m 0644 "$stage/unit" "$unit"
systemctl daemon-reload
systemctl enable "$unit_name"
systemctl start --no-block "$unit_name"
committed=1
echo "INSTALLED: startup check queued; verify the service result and journal."
