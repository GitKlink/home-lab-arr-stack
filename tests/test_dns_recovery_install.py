"""Exercise the real installer flow in a redirected, temporary filesystem.

Only destination paths are redirected. Docker/IP/systemctl are fake; install,
Python, config parsing, preflight and the Bash cleanup trap actually execute.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.repo = self.root / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "systemd").mkdir()
        shutil.copy(ROOT / "scripts/arr-dns-recover.py", self.repo / "scripts")
        shutil.copy(ROOT / "systemd/arr-dns-recover.service", self.repo / "systemd")
        self.stack = self.root / "stack with spaces"
        self.stack.mkdir()
        (self.stack / "docker-compose.arr-stack.yml").touch()
        text = (ROOT / "scripts/install-dns-recovery.sh").read_text()
        for path in ("/usr/local/sbin/arr-dns-recover", "/etc/systemd/system/arr-dns-recover.service", "/etc/arr-stack"):
            destination = self.root / path.lstrip("/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            text = text.replace(path, str(destination))
        # CI may use an unprivileged runner. The production root guard remains;
        # this copy tests only temporary destinations and removes chown flags.
        text = text.replace('if [[ $EUID -ne 0 ]]; then', 'if false; then')
        text = text.replace('-o root -g root ', '')
        self.installer = self.repo / "scripts/install-dns-recovery.sh"
        self.installer.write_text(text)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "systemctl.log"
        stub = """#!/usr/bin/python3
import os,sys
sys.path.insert(0, os.environ['DNS_TEST_IMPORT'])
from test_dns_recovery import FakeHost
host=FakeHost()
host.docker_ready=not os.environ.get('DNS_TEST_FAIL_PREFLIGHT')
result=host.run([os.path.basename(sys.argv[0])] + sys.argv[1:], timeout=5)
print(result.stdout)
sys.exit(result.returncode)
"""
        for name in ("docker", "ip"):
            self.executable(name, stub)
        self.executable("systemctl", """#!/usr/bin/python3
import json,os,sys
with open(os.environ['DNS_TEST_SYSTEMCTL_LOG'],'a') as stream:
 stream.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1]=='cat': sys.exit(0 if sys.argv[2]=='docker_serv.service' else 1)
if sys.argv[1]=='enable' and os.environ.get('DNS_TEST_FAIL_ENABLE'): sys.exit(1)
""")
        real_install = shutil.which("install")
        self.executable("install", """#!/usr/bin/python3
import os,subprocess,sys
result=subprocess.run([""" + repr(real_install) + """]+sys.argv[1:])
if os.environ.get('DNS_TEST_FAIL_UNIT_WRITE') and sys.argv[-1].endswith('/etc/systemd/system/arr-dns-recover.service'):
 sys.exit(1)
sys.exit(result.returncode)
""")
        self.env = dict(os.environ, PATH=str(self.bin) + ":" + os.environ["PATH"],
                        DNS_TEST_IMPORT=str(ROOT / "tests"), DNS_TEST_SYSTEMCTL_LOG=str(self.calls))

    def executable(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)

    def run_install(self, **changes):
        return subprocess.run(["bash", str(self.installer), "192.0.2.50", str(self.stack)],
                              env=dict(self.env, **changes), text=True, capture_output=True, timeout=20)

    def targets(self):
        return [self.root / p for p in ("usr/local/sbin/arr-dns-recover", "etc/arr-stack/dns-recovery.ini",
                                       "etc/systemd/system/arr-dns-recover.service")]

    def test_success_installs_snapshots_private_config_and_enables_unit(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for target, mode in zip(self.targets(), (0o750, 0o600, 0o644)):
            self.assertEqual(target.stat().st_mode & 0o777, mode)
        self.assertIn(str(self.stack), self.targets()[1].read_text())
        self.assertIn('["enable", "arr-dns-recover.service"]', self.calls.read_text())
        self.assertIn('["start", "--no-block", "arr-dns-recover.service"]', self.calls.read_text())

    def test_unhealthy_preflight_writes_no_installed_files(self):
        result = self.run_install(DNS_TEST_FAIL_PREFLIGHT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(p.exists() for p in self.targets()))
        self.assertNotIn('["enable"', self.calls.read_text())

    def test_enable_failure_and_partial_write_are_rolled_back(self):
        for failure in ("DNS_TEST_FAIL_ENABLE", "DNS_TEST_FAIL_UNIT_WRITE"):
            with self.subTest(failure=failure):
                result = self.run_install(**{failure: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(p.exists() for p in self.targets()))
                self.assertIn("newly installed files were removed", result.stderr)

    def test_existing_target_is_not_overwritten(self):
        target = self.targets()[0]
        target.write_text("existing installation")
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(), "existing installation")


if __name__ == "__main__":
    unittest.main(verbosity=2)
