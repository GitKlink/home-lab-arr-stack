# Pi-hole boot recovery on UGREEN

Optional, machine-local systemd service for an existing deployment. Install only
when Pi-hole has been observed running after boot without its `arr-stack`
attachment. This is a recovery workaround; the initial startup error and its
cause have not been captured. A delay alone is not a demonstrated fix.

## What runs

Once each boot, after UGREEN's `docker_serv.service` startup completes, the
helper waits up to **300 seconds** for deployment storage, the configured NAS
IPv4 address, Docker, the existing network and existing DNS containers. Every
command uses the remaining deadline as its maximum timeout. systemd has a
330-second final limit. There is no recurring timer or automatic retry after
that boot check finishes.

The helper checks both containers' network mode and reserved addresses before
mutating either. It can start existing `created`/`exited` DNS containers. For a
running Pi-hole missing its network attachment, it reconnects at the registered
`172.20.0.5` with alias `pihole`. If active TCP/UDP 53 mappings are still missing,
it attempts **one Pi-hole-only restart**. DNScrypt must be attached at
`172.20.0.6` and healthy. Success requires a real IPv4 answer for `example.com`
through the **published NAS IP**, queried from inside Pi-hole.

Healthy containers are left untouched. The helper does not create containers or
networks, change IPAM, disconnect other endpoints, recreate Traefik, pull images,
modify volumes, or edit vendor services. Unexpected configured bindings, network
modes or assigned addresses stop recovery. Missing prerequisites and failed
connects are retried only until the deadline. See [IP reservations](IP-RESERVATIONS.md).

This does not prevent UGREEN's per-container updater from deleting VPN-networked
download clients during failed rebuilds. Manage those services through Git and
their defining Compose file.

## Installed files

| Path | Purpose |
|---|---|
| `/usr/local/sbin/arr-dns-recover` | Root-owned snapshot of the Python helper, mode 0750 |
| `/etc/arr-stack/dns-recovery.ini` | Private NAS IP and absolute Compose path, mode 0600 |
| `/etc/systemd/system/arr-dns-recover.service` | Enabled boot unit, mode 0644 |

Python 3's standard library, `ip`, Docker CLI and systemd are required. The unit
is specific to UGREEN's `docker_serv.service`. Other distributions need a
reviewed unit for their own Docker service. The installed helper runs as root
to access Docker. It reads the INI as data and never sources `.env` or shell
configuration. No tokens are needed. Actual host details stay outside Git.

Git versions the source, installer, unit and tests. **Pulling or changing branches
does not update the installed copies.** After a reviewed helper change, disable
and uninstall the old installation, then run the installer again while DNS is
healthy. Recheck the unit after firmware upgrades, which may alter local files
or the vendor startup service.

## Install from the feature branch

Run in the **NAS SSH terminal**, from your checkout on the reviewed branch.
Replace `NAS_IP` and `/absolute/path/to/arr-stack` below with your deployment's
values. First confirm DNS works through that NAS IP:

```bash
sudo docker exec pihole dig @NAS_IP example.com A +short +time=3 +tries=1
```

**Check:** IPv4 answers, both DNS containers running/healthy, and
`systemctl is-active docker_serv.service` reports `active`. If DNS is broken,
diagnose or restore it before installing; the installer deliberately requires a
healthy read-only preflight.

Install the machine-local service:

```bash
sudo bash scripts/install-dns-recovery.sh NAS_IP /absolute/path/to/arr-stack
```

**Check:** `OK:` from the read-only preflight, followed by `INSTALLED:`. Existing
files/units cause a refusal rather than an overwrite. An install-command failure
removes newly installed files. An asynchronously queued service that later fails
stays installed for diagnosis. Inspect its journal before retrying.

Inspect the first run:

```bash
sudo systemctl --no-pager --full status arr-dns-recover.service
sudo journalctl -u arr-dns-recover.service -b --no-pager -n 40
sudo systemctl is-enabled arr-dns-recover.service
sudo /usr/local/sbin/arr-dns-recover --check
```

**Check:** service `active (exited)`, successful exit, `OK:` in this boot's journal,
`enabled`, and a successful read-only check. `active (exited)` means the one-time
check finished; it does not continuously monitor DNS. On a healthy first install,
there should be no start/connect/restart messages. If the job is still activating,
inspect its journal and let its bounded check finish.

## Verify before merging

1. Run the automated recovery tests (no container changes):

   ```bash
   python3 tests/test_dns_recovery.py
   python3 tests/test_dns_recovery_install.py
   ```

   Check: all tests pass. These simulate missing attachments and startup races;
   they do not prove real NAS startup behavior. CI also runs them through BATS.

2. Confirm the healthy initial service run and browser access to local services.
   From a separate LAN client, query the NAS DNS address directly; the helper's
   container-to-NAS probe cannot prove the client firewall/routing path:

   ```powershell
   nslookup -type=A example.com. NAS_IP
   nslookup -type=A sonarr.lan. NAS_IP
   ```

   The trailing dots prevent Windows from appending its DNS search suffix;
   `-type=A` checks IPv4 explicitly. Check: external answers and the expected
   local Traefik address.

3. During an agreed interruption window, restart the NAS through UGREEN's normal
   restart action. SSH will disconnect and DNS may be unavailable while it boots.
   After reconnecting, repeat the service/journal/read-only and client checks.
   Check this boot's journal, not an older success. Do not disconnect Pi-hole
   manually on the live LAN just to manufacture a failure.

4. Record the branch commit, CI results, initial run and post-reboot results in
   the PR. Keep the PR unmerged until NAS verification passes. A later UPS test
   can check the full power-restoration path; a normal reboot does not reproduce
   every storage/network timing condition of a long outage.

## Failure and uninstall

If the deadline expires, the service becomes failed and stops retrying. Inspect
the current-boot journal, storage, IP and Docker metadata. It will not fix an IP
conflict, changed Compose configuration, missing container or upstream outage by
rebuilding the stack. After fixing the cause, rerun the check:

```bash
sudo systemctl restart arr-dns-recover.service
sudo journalctl -u arr-dns-recover.service -b --no-pager -n 40
```

To remove this installation, stop the helper before removing its files:

```bash
sudo systemctl disable --now arr-dns-recover.service
sudo rm -- /etc/systemd/system/arr-dns-recover.service /usr/local/sbin/arr-dns-recover /etc/arr-stack/dns-recovery.ini
sudo systemctl daemon-reload
sudo systemctl reset-failed arr-dns-recover.service
```

The final `reset-failed` may report the unit is no longer loaded; the unit files
are already removed. DNS containers keep running, and their network attachment
and volumes are unchanged. Uninstalling removes automatic boot recovery.
