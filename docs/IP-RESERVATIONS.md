# IP reservations

Use this register before adding or moving a service on the shared `arr-stack` network. An address remains reserved when its service is stopped, optional or not installed.

Checked against [Pharkie/ultimate-arr-stack at aab27ba](https://github.com/Pharkie/ultimate-arr-stack/tree/aab27ba04b337ae844d526171bd15bccdfc40239) on 2026-11-09. This is a reviewed snapshot, not an automatic upstream sync. Recheck upstream allocations before an upstream merge or a new allocation.

## Shared bridge: arr-stack

Network: `172.20.0.0/24`. Gateway: `172.20.0.1`. Docker's dynamic allocation pool is `172.20.0.128/25`; keep fixed service addresses below `.128`. Preserve that pool when changing the network.

| IP address | Reserved owner | Reservation source | Definition |
|---|---|---|---|
| `172.20.0.1` | Bridge gateway | Upstream | [Core network IPAM](../docker-compose.arr-stack.yml) |
| `172.20.0.2` | Traefik | Upstream | [Traefik Compose](../docker-compose.traefik.yml) |
| `172.20.0.3` | Gluetun | Upstream | [Core Compose](../docker-compose.arr-stack.yml) |
| `172.20.0.4` | Jellyfin | Upstream | [Core Compose](../docker-compose.arr-stack.yml) |
| `172.20.0.5` | Pi-hole | Upstream | [Core Compose](../docker-compose.arr-stack.yml) |
| `172.20.0.6` | dnscrypt-proxy | Upstream | [Core Compose](../docker-compose.arr-stack.yml) |
| `172.20.0.8` | Seerr | Upstream | [Core Compose](../docker-compose.arr-stack.yml) |
| `172.20.0.9` | Bazarr | Upstream | [Core Compose](../docker-compose.arr-stack.yml) |
| `172.20.0.10` | Sonarr | Upstream | [Core Compose](../docker-compose.arr-stack.yml) |
| `172.20.0.11` | Radarr | Upstream | [Core Compose](../docker-compose.arr-stack.yml) |
| `172.20.0.12` | Cloudflared | Upstream; reserved even without a tunnel | [Cloudflared Compose](../docker-compose.cloudflared.yml) |
| `172.20.0.13` | Uptime Kuma | Upstream; optional | [Utilities Compose](../docker-compose.utilities.yml) |
| `172.20.0.14` | duc | Upstream; optional | [Utilities Compose](../docker-compose.utilities.yml) |
| `172.20.0.15` | Beszel | Upstream; optional | [Utilities Compose](../docker-compose.utilities.yml) |
| `172.20.0.16` | DIUN | Legacy upstream reservation retained by this fork | [Utilities Compose](../docker-compose.utilities.yml) |
| `172.20.0.17` | Lidarr | Fork addition | [Core Compose](../docker-compose.arr-stack.yml), [Traefik backend](../traefik/dynamic/local-services.yml) |
| `172.20.0.20` | External project | Reserved conservatively from upstream guidance; local owner unverified | [Cross-stack instructions](../CLAUDE.md#cross-stack-a-neighbouring-compose-project-shares-this-network) |

DIUN is removed in the pinned upstream revision, but this fork still defines it. Do not recycle `.16` while that definition remains. Upstream's instructions mention an external project at `.20`; this does not establish that the same project exists on every NAS. Keep the address reserved until the private deployment inventory is checked.

Lidarr used `.12` in the initial preservation commit. That overlaps Cloudflared's reservation. The fork now assigns Lidarr `.17` and routes its proxy backend to `http://172.20.0.17:8686`. The existing `arr-stack_lidarr-config` volume name remains unchanged.

This table records configuration reservations; it does not itself reserve addresses in Docker or configure router DHCP. An empty address in `docker network inspect` is not proof that the address is available: stopped services, other Compose projects and upstream definitions may still claim it. Addresses omitted from this table require an inventory check before allocation.

## Other network scopes

- **VPN network:** `10.8.1.0/24`. Defined by [Core Compose](../docker-compose.arr-stack.yml); its gateway is not explicitly pinned there.
- **VPN clients:** qBittorrent, SABnzbd, Prowlarr and FlareSolverr share Gluetun's network namespace; do not give them separate bridge IPs for this purpose.
- **Traefik LAN address:** `${TRAEFIK_LAN_IP}` belongs to the separate `traefik-lan` macvlan network. The actual LAN address is private deployment configuration, not a `172.20.0.x` bridge reservation.
- **Tailscale:** uses host networking and does not reserve an address on `arr-stack`.
- Containers attached to `arr-stack` without an explicit static address use Docker's dynamic pool.

## Adding or changing an allocation

Start in an SSH session on the NAS, in the checkout for this repository. These checks do not change containers.

1. Compare the intended address with this table and all five `docker-compose*.yml` files, including optional Cloudflared and utilities. Check the current upstream definitions too. **Check:** the address has no other reserved owner. If it does, select a different address.
2. Read the deployed bridge inventory:

   ```bash
   sudo docker network inspect arr-stack --format '{{range .Containers}}{{println .Name .IPv4Address}}{{end}}'
   ```

   **Check:** no attached container already uses the intended address. Also inspect private Compose definitions for other projects and stopped services; their reservations may be absent from this output. Stop if ownership is uncertain.
3. On a feature branch, edit the service's Compose address, its proxy backend or other fixed-IP references, and this table together. Preserve existing named-volume names. **Check:** the service and backend agree on the new address, and another service's reservation has not changed.
4. From the repository root, run the existing cross-file conflict check:

   ```bash
   bash -c 'source scripts/lib/check-conflicts.sh; check_conflicts'
   ```

   **Check:** exit status zero, with no error output. An `ERROR: IP ... used across multiple files` means there is still a duplicate. The checker covers tracked Compose literals; it does not query upstream, inspect private projects or enforce this Markdown table.
5. Run the project's relevant tests and deploy only the affected services from the feature branch. Follow [the NAS deployment rules](../CLAUDE.md#deploying-to-the-nas) before merging.

Done when the register, all affected definitions and the deployed service agree, the duplicate-IP check passes, and NAS service/proxy verification succeeds. Documentation and Git changes alone do not change the running container's IP.
