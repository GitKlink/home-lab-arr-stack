#!/usr/bin/python3
"""One bounded boot recovery for the existing Arr-stack DNS containers."""

import argparse
import configparser
import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import time


NETWORK = "arr-stack"
ADDRESSES = {"pihole": "172.20.0.5", "dnscrypt-proxy": "172.20.0.6"}
INSPECT = (
    '{"state":{{json .State}},"mode":{{json .HostConfig.NetworkMode}},'
    '"bindings":{{json .HostConfig.PortBindings}},'
    '"networks":{{json .NetworkSettings.Networks}},'
    '"ports":{{json .NetworkSettings.Ports}}}'
)


class StopRecovery(Exception):
    """Unsafe configuration or exhausted deadline: stop without broad repair."""


class NotReady(Exception):
    """A boot prerequisite may become ready before the deadline."""


def read_config(path):
    parser = configparser.ConfigParser(interpolation=None)
    with open(path, encoding="utf-8") as stream:
        parser.read_file(stream)
    address = str(ipaddress.IPv4Address(parser["dns-recovery"]["nas_ip"]))
    compose = Path(parser["dns-recovery"]["compose_path"])
    if not compose.is_absolute() or "\n" in str(compose):
        raise ValueError("compose_path must be an absolute path without newlines")
    if ipaddress.IPv4Address(address).is_unspecified or ipaddress.IPv4Address(address).is_multicast:
        raise ValueError("nas_ip must be a unicast IPv4 address")
    return address, compose


class Recovery:
    def __init__(self, nas_ip, compose, check=False, wait_seconds=300,
                 runner=subprocess.run, clock=time.monotonic, sleep=time.sleep):
        self.nas_ip = nas_ip
        self.compose = compose
        self.check = check
        self.runner = runner
        self.clock = clock
        self.sleep = sleep
        self.deadline = clock() + wait_seconds
        self.port_restart_attempted = False
        self.last_message = None

    def log(self, message):
        if message != self.last_message:
            print("[arr-dns-recover] " + message, flush=True)
            self.last_message = message

    def command(self, args, timeout=5):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise StopRecovery("Recovery deadline reached")
        try:
            result = self.runner(args, capture_output=True, text=True,
                                 timeout=min(timeout, remaining), check=False)
        except (subprocess.TimeoutExpired, OSError):
            raise NotReady("Command unavailable or timed out: " + " ".join(args[:2])) from None
        if result.returncode:
            # Never dump inspect output, environment variables or command stderr.
            raise NotReady("Command failed: " + " ".join(args[:2]))
        return result.stdout

    def docker_json(self, args):
        try:
            return json.loads(self.command(["docker"] + args))
        except (ValueError, TypeError):
            raise NotReady("Docker returned invalid metadata") from None

    def inspect(self, name):
        return self.docker_json(["inspect", "--format", INSPECT, name])

    def has_ports(self, ports):
        return all(any(p.get("HostIp") == self.nas_ip and p.get("HostPort") == "53"
                       for p in (ports.get("53/" + protocol) or []))
                   for protocol in ("tcp", "udp"))

    def validate(self, name, container):
        if container["mode"] != NETWORK:
            raise StopRecovery(name + " has an unexpected network mode; inspect manually")
        endpoint = (container["networks"] or {}).get(NETWORK) or {}
        # Stopped containers may have no runtime address. A different assigned
        # or requested static IP is configuration drift, not a boot race.
        for address in (endpoint.get("IPAddress"),
                        (endpoint.get("IPAMConfig") or {}).get("IPv4Address")):
            if address and address != ADDRESSES[name]:
                raise StopRecovery(name + " has an unexpected IP; no readdressing attempted")
        if name == "pihole" and not self.has_ports(container["bindings"] or {}):
            raise StopRecovery("Pi-hole is not configured to publish TCP/UDP 53 on the NAS IP")

    def start_if_stopped(self, name, container):
        status = container["state"]["Status"]
        if status == "running":
            return
        if self.check or status not in ("created", "exited"):
            raise NotReady(name + " is " + status + "; no action taken")
        self.log("Starting existing " + name)
        self.command(["docker", "start", name], timeout=20)
        raise NotReady("Waiting for " + name + " after start")

    def attempt(self):
        if not self.compose.is_file():
            raise NotReady("Waiting for deployment storage and Compose file")
        interfaces = json.loads(self.command(["ip", "-j", "-4", "addr", "show"]))
        if not any(a.get("local") == self.nas_ip for i in interfaces for a in i.get("addr_info", [])):
            raise NotReady("Waiting for the configured NAS IP")
        self.command(["docker", "info"], timeout=5)
        ipam = self.docker_json(["network", "inspect", "--format", "{{json .IPAM.Config}}", NETWORK])
        if not any(all(ipaddress.IPv4Address(a) in ipaddress.ip_network(n["Subnet"])
                       for a in ADDRESSES.values()) for n in ipam if n.get("Subnet")):
            raise StopRecovery("Existing arr-stack subnet does not contain the reserved DNS addresses")
        containers = {name: self.inspect(name) for name in ADDRESSES}
        # Validate both before starting or reconnecting either container.
        for name, container in containers.items():
            self.validate(name, container)
        for name in ("dnscrypt-proxy", "pihole"):
            self.start_if_stopped(name, containers[name])
        proxy = containers["dnscrypt-proxy"]
        proxy_endpoint = (proxy["networks"] or {}).get(NETWORK) or {}
        if proxy_endpoint.get("IPAddress") != ADDRESSES["dnscrypt-proxy"]:
            raise NotReady("Waiting for dnscrypt-proxy attachment; no proxy network changes attempted")
        pihole = containers["pihole"]
        endpoint = (pihole["networks"] or {}).get(NETWORK)
        if endpoint is None:
            if self.check:
                raise NotReady("Pi-hole attachment is missing")
            self.log("Restoring Pi-hole attachment at " + ADDRESSES["pihole"])
            self.command(["docker", "network", "connect", "--ip", ADDRESSES["pihole"],
                          "--alias", "pihole", NETWORK, "pihole"], timeout=15)
            raise NotReady("Waiting for Pi-hole after attachment repair")
        if endpoint.get("IPAddress") != ADDRESSES["pihole"]:
            raise NotReady("Waiting for Pi-hole's reserved runtime address")
        if not self.has_ports(pihole["ports"] or {}):
            if not self.check and not self.port_restart_attempted:
                self.port_restart_attempted = True
                self.log("Published DNS ports missing; attempting one Pi-hole restart")
                self.command(["docker", "restart", "pihole"], timeout=30)
            raise NotReady("Waiting for published TCP/UDP DNS ports")
        if proxy["state"].get("Health", {}).get("Status") != "healthy":
            raise NotReady("Waiting for dnscrypt-proxy health")
        answers = self.command(["docker", "exec", "pihole", "dig", "@" + self.nas_ip,
                                "example.com", "A", "+short", "+time=2", "+tries=1"], timeout=5)
        for line in answers.splitlines():
            try:
                ipaddress.IPv4Address(line.strip())
                return
            except ValueError:
                pass
        raise NotReady("Published DNS query returned no IPv4 answer")

    def run(self):
        self.log("Read-only check" if self.check else "Starting bounded boot recovery (maximum 300 seconds)")
        while self.clock() < self.deadline:
            try:
                self.attempt()
                self.log("OK: DNS attachment, published ports and NAS-address DNS query passed")
                return 0
            except NotReady as error:
                self.log(str(error))
                if self.check:
                    return 1
                self.sleep(max(0, min(5, self.deadline - self.clock())))
            except StopRecovery as error:
                self.log("STOP: " + str(error))
                return 1
        self.log("FAILED: recovery deadline reached; inspect this boot's journal")
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/arr-stack/dns-recovery.ini")
    parser.add_argument("--check", action="store_true", help="One read-only check; never modify containers")
    args = parser.parse_args()
    try:
        address, compose = read_config(args.config)
        return Recovery(address, compose, check=args.check).run()
    except (OSError, ValueError, KeyError, TypeError, configparser.Error) as error:
        # No raw configuration values or Docker output in error messages.
        print("[arr-dns-recover] STOP: invalid config or metadata (" + type(error).__name__ + ")", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
