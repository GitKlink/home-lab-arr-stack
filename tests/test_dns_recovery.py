"""Recovery behavior with a fake Docker daemon and clock; no NAS mutations."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dns_recovery", ROOT / "scripts/arr-dns-recover.py")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
NAS_IP = "192.0.2.50"


class FakeHost:
    def __init__(self):
        self.time = 0
        self.calls = []
        self.mutations = []
        self.ip_ready = True
        self.docker_ready = True
        self.network_exists = True
        self.connect_ok = True
        self.connect_restores_ports = True
        self.restart_restores_ports = True
        self.answers = "example.com.\n203.0.113.1\n"
        self.containers = {}
        for name, address in recovery.ADDRESSES.items():
            ports = {"53/" + p: [{"HostIp": NAS_IP, "HostPort": "53"}] for p in ("tcp", "udp")}
            self.containers[name] = {
                "state": {"Status": "running", "Health": {"Status": "healthy"}},
                "mode": "arr-stack", "bindings": copy.deepcopy(ports), "ports": ports,
                "networks": {"arr-stack": {"IPAddress": address, "IPAMConfig": {"IPv4Address": address}}},
            }

    def clock(self):
        return self.time

    def sleep(self, seconds):
        self.time += seconds

    def run(self, args, **kwargs):
        self.calls.append(args)
        self.assert_timeout(kwargs["timeout"])
        result = ""
        failed = False
        if args[0] == "ip":
            result = [{"addr_info": [{"local": NAS_IP}] if self.ip_ready else []}]
        elif args[1] == "info":
            failed = not self.docker_ready
        elif args[1:3] == ["network", "inspect"]:
            failed = not self.network_exists
            result = [{"Subnet": "172.20.0.0/24"}]
        elif args[1] == "inspect":
            failed = args[-1] not in self.containers
            result = self.containers.get(args[-1])
        elif args[1:3] == ["network", "connect"]:
            self.mutations.append(args)
            failed = not self.connect_ok
            if not failed:
                self.containers["pihole"]["networks"]["arr-stack"] = {"IPAddress": args[args.index("--ip") + 1]}
                if self.connect_restores_ports:
                    self.containers["pihole"]["ports"] = copy.deepcopy(self.containers["pihole"]["bindings"])
        elif args[1] in ("start", "restart"):
            self.mutations.append(args)
            self.containers[args[-1]]["state"]["Status"] = "running"
            if args[1] == "restart" and self.restart_restores_ports:
                self.containers["pihole"]["ports"] = copy.deepcopy(self.containers["pihole"]["bindings"])
        elif args[1] == "exec":
            result = self.answers
        else:
            raise AssertionError("Unexpected command: " + repr(args))
        return subprocess.CompletedProcess(args, int(failed), result if isinstance(result, str) else json.dumps(result), "")

    def assert_timeout(self, timeout):
        if not 0 < timeout <= 30 - self.time:
            raise AssertionError("Command timeout exceeds the remaining recovery budget")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.compose = Path(self.folder.name) / "compose.yml"
        self.compose.touch()
        self.host = FakeHost()

    def run_helper(self, check=False):
        helper = recovery.Recovery(NAS_IP, self.compose, check=check, wait_seconds=30,
                                   runner=self.host.run, clock=self.host.clock, sleep=self.host.sleep)
        return helper.run()

    def detach(self):
        self.host.containers["pihole"]["networks"] = {}
        self.host.containers["pihole"]["ports"] = {}

    def test_healthy_stack_is_untouched(self):
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.host.mutations, [])
        self.assertIn(["docker", "exec", "pihole", "dig", "@" + NAS_IP,
                       "example.com", "A", "+short", "+time=2", "+tries=1"], self.host.calls)

    def test_missing_attachment_is_repaired_at_reserved_ip(self):
        self.detach()
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.host.mutations, [["docker", "network", "connect", "--ip", "172.20.0.5",
                                              "--alias", "pihole", "arr-stack", "pihole"]])

    def test_missing_ports_after_connect_restart_only_pihole(self):
        self.detach()
        self.host.connect_restores_ports = False
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.host.mutations[-1], ["docker", "restart", "pihole"])
        self.assertEqual(len(self.host.mutations), 2)

    def test_restart_attempt_is_limited_to_one(self):
        self.host.containers["pihole"]["ports"] = {}
        self.host.restart_restores_ports = False
        self.assertEqual(self.run_helper(), 1)
        self.assertEqual(self.host.mutations, [["docker", "restart", "pihole"]])
        self.assertEqual(self.host.time, 30)

    def test_read_only_check_never_repairs(self):
        for failure in ("attachment", "ports", "stopped"):
            with self.subTest(failure=failure):
                self.host = FakeHost()
                if failure == "attachment":
                    self.detach()
                elif failure == "ports":
                    self.host.containers["pihole"]["ports"] = {}
                else:
                    self.host.containers["pihole"]["state"]["Status"] = "exited"
                self.assertEqual(self.run_helper(check=True), 1)
                self.assertEqual(self.host.mutations, [])
                self.assertEqual(self.host.time, 0)

    def test_waits_for_storage_ip_docker_network_and_containers(self):
        for prerequisite in ("storage", "ip", "docker", "network", "container"):
            with self.subTest(prerequisite=prerequisite):
                self.host = FakeHost()
                self.compose.touch()
                self.detach()
                if prerequisite == "storage":
                    self.compose.unlink()
                elif prerequisite == "ip":
                    self.host.ip_ready = False
                elif prerequisite == "docker":
                    self.host.docker_ready = False
                elif prerequisite == "network":
                    self.host.network_exists = False
                else:
                    del self.host.containers["dnscrypt-proxy"]
                self.assertEqual(self.run_helper(), 1)
                self.assertEqual(self.host.mutations, [])

    def test_ip_appearing_later_allows_recovery(self):
        original = self.host.run
        self.host.ip_ready = False
        self.detach()
        def delayed(args, **kwargs):
            self.host.ip_ready = self.host.time >= 10
            return original(args, **kwargs)
        self.host.run = delayed
        self.assertEqual(self.run_helper(), 0)
        self.assertGreaterEqual(self.host.time, 10)

    def test_invalid_modes_addresses_or_bindings_fail_without_mutation(self):
        for change in ("mode", "pihole_ip", "proxy_ip", "static_ip", "bindings"):
            with self.subTest(change=change):
                self.host = FakeHost()
                pihole = self.host.containers["pihole"]
                if change == "mode":
                    pihole["mode"] = "host"
                elif change == "pihole_ip":
                    pihole["networks"]["arr-stack"]["IPAddress"] = "172.20.0.99"
                elif change == "static_ip":
                    pihole["networks"]["arr-stack"]["IPAMConfig"]["IPv4Address"] = "172.20.0.99"
                elif change == "proxy_ip":
                    self.host.containers["dnscrypt-proxy"]["networks"]["arr-stack"]["IPAddress"] = "172.20.0.99"
                else:
                    pihole["bindings"] = {}
                self.assertEqual(self.run_helper(), 1)
                self.assertEqual(self.host.mutations, [])

    def test_existing_stopped_containers_are_started(self):
        for c in self.host.containers.values():
            c["state"]["Status"] = "exited"
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.host.mutations, [["docker", "start", "dnscrypt-proxy"], ["docker", "start", "pihole"]])

    def test_paused_container_is_not_unpaused(self):
        self.host.containers["pihole"]["state"]["Status"] = "paused"
        self.assertEqual(self.run_helper(), 1)
        self.assertEqual(self.host.mutations, [])

    def test_connect_failure_never_disconnects_other_containers(self):
        self.detach()
        self.host.connect_ok = False
        self.assertEqual(self.run_helper(), 1)
        self.assertTrue(self.host.mutations)
        self.assertTrue(all(c[1:3] == ["network", "connect"] for c in self.host.mutations))

    def test_no_answer_and_unhealthy_proxy_do_not_cause_restart_loop(self):
        for failure in ("answer", "health"):
            with self.subTest(failure=failure):
                self.host = FakeHost()
                if failure == "answer":
                    self.host.answers = ";; status: REFUSED\nexample.com.\n999.1.2.3\n"
                else:
                    self.host.containers["dnscrypt-proxy"]["state"]["Health"]["Status"] = "unhealthy"
                self.assertEqual(self.run_helper(), 1)
                self.assertEqual(self.host.mutations, [])

    def test_command_deadline_includes_subprocess_timeout(self):
        def blocked(args, **kwargs):
            self.host.sleep(kwargs["timeout"])
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        self.host.run = blocked
        self.assertEqual(self.run_helper(), 1)
        self.assertEqual(self.host.time, 30)

    def test_config_is_data_not_shell_code(self):
        config = Path(self.folder.name) / "config.ini"
        config.write_text("[dns-recovery]\nnas_ip = " + NAS_IP + "\ncompose_path = /srv/$(touch should-not-exist)/compose.yml\n")
        address, compose = recovery.read_config(config)
        self.assertEqual(address, NAS_IP)
        self.assertIn("$(touch", str(compose))
        config.write_text("[dns-recovery]\nnas_ip = bad\ncompose_path = relative\n")
        with self.assertRaises(ValueError):
            recovery.read_config(config)


if __name__ == "__main__":
    unittest.main(verbosity=2)
