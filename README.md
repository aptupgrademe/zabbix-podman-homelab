# Zabbix in rootless Podman for a home network

Zabbix 7.0 LTS in four rootless Podman containers, run as ordinary systemd user services
(Quadlet), plus a small script that creates the monitoring from a YAML file through the API:
ping and port checks for the devices in the LAN, web checks and certificate expiry for
external sites, and mail when something stops answering.

Background, pitfalls and a tour of the interface:
[My Wife Beat My Monitoring: Zabbix in Containers Now Watches My Home Network](https://www.apt-upgrade.me/2026/11/13/zabbix-in-containers-watches-my-home-network/)

## How it works

**Rootless Podman.** Podman runs containers like Docker, but without a background service running
as root: every container is just a process of your normal user. If something breaks out of a
container, it has your rights, not root's. The images are the same as Docker's.

**Quadlet.** Instead of a `docker-compose.yml`, each container is a small file ending in
`.container` in `~/.config/containers/systemd/`, written like a systemd unit. On
`systemctl --user daemon-reload`, Quadlet (part of Podman since 4.4) turns each file into an
ordinary systemd service. From then on everything you know from systemd works: start order and
dependencies, restart after a crash, start at boot, `systemctl --user status zabbix-server`,
`journalctl --user -u zabbix-server`. The files in `quadlet/` are commented line by line.

The four sections of such a file:

| Section | Meaning |
|---|---|
| `[Unit]` | description and order (`Requires=`, `After=`: start only once the database runs) |
| `[Container]` | what Podman runs: image with a fixed version, network, environment, secret, capabilities, limits |
| `[Service]` | how systemd treats it, here `Restart=always` |
| `[Install]` | when it starts: at boot with the user's services (`default.target`) |

**The four containers.**

| Container | Job | Notes |
|---|---|---|
| `zabbix-db` | PostgreSQL: configuration, measurements, history | listens on 127.0.0.1 only, data in `~/zabbix/pgdata` |
| `zabbix-server` | runs the checks, evaluates triggers, sends mail | needs `NET_RAW` for ping |
| `zabbix-web` | web UI on port 8080 | talks to the database only |
| `zabbix-agent` | measures the home server itself | sees the host file system read-only under `/hostfs` |

**Design choices.** Host network instead of NAT, so pings and TCP checks reach the LAN directly.
Passwords never in files: the database password is a Podman secret mounted under `/run/secrets/`.
Memory and CPU limits per container, so monitoring never gets in the way of the server's real job
(all four together use about 180 MB in practice). `loginctl enable-linger` once, so the user
services start at boot without anyone logging in.

## Files

| File | Purpose |
|---|---|
| `quadlet/zabbix-{db,server,web,agent}.container` | PostgreSQL 17, Zabbix server, web UI (port 8080) and agent 2, host network, memory limits |
| `provision.py` | creates hosts, checks and triggers from `hosts.yml` through the API (skips hosts that exist) |
| `zapi.py` | minimal JSON-RPC client; changes the default Admin password on the first run |
| `hosts.example.yml` | example inventory with placeholders – copy to `hosts.yml` (ignored by Git) |
| `backup.sh` | nightly `pg_dump`, verified before it replaces anything, mail on failure |

## Where Zabbix keeps what

Everything you configure in the web UI – hosts, checks, triggers, templates, actions – lives in
the **database**, together with all measurements. The files here only describe how the containers
run, and `provision.py` is a one-off way to fill an empty Zabbix, not a living source of truth:
changes made in the UI afterwards exist only in the database. For a restore you need the database
dump, the Quadlet files and the Podman secret. If you want Git to be the truth instead, look at the
Ansible collection `community.zabbix` and stop editing in the UI.

## Setup

```bash
# 1. user services must start without a login
loginctl enable-linger "$USER"

# 2. database password as a Podman secret, Admin password for the API scripts
openssl rand -base64 24 | podman secret create zabbix-db-pw -
install -d -m 700 ~/.config/zabbix
read -rs -p 'Zabbix Admin password: ' p; printf '%s' "$p" > ~/.config/zabbix/admin.pass; chmod 600 ~/.config/zabbix/admin.pass; unset p; echo

# 3. containers
mkdir -p ~/zabbix/pgdata
cp quadlet/*.container ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user start zabbix-db && sleep 10 && systemctl --user start zabbix-server zabbix-web zabbix-agent

# 4. monitoring (web UI on http://<host>:8080)
cp hosts.example.yml hosts.yml && $EDITOR hosts.yml
python3 provision.py hosts.yml
```

Mail delivery is configured in the UI (*Alerts → Media types → Email*, then the user's media and
the action *Report problems to Zabbix administrators*, which is disabled by default).

## Pitfalls I hit

- **Ping needs `NET_RAW`.** `fping` in the server image carries a file capability; without
  `AddCapability=NET_RAW` the kernel refuses to start it (`Operation not permitted`).
- **Rootless ping** works because Debian sets `net.ipv4.ping_group_range = 0 2147483647`.
- **Disk metrics** only appear if `/dev/<disk>` exists in the agent container – hand the devices in
  read-only. Check the names with `lsblk`; they can change after a reinstall.
- **File systems** of the host show up under `/hostfs/…`; filter Podman's overlay mounts with the
  host macro `{$VFS.FS.FSNAME.MATCHES}`.
- **Time zone and date format:** the UI starts on UTC and a 12-hour clock – set them under
  *Administration → General*.
- **Host names:** the technical name allows only `[A-Za-z0-9 ._-]`, so `provision.py` keeps the
  original as the visible name.
- **SSD write load:** `synchronous_commit=off`, `wal_writer_delay=2000ms`, `checkpoint_timeout=15min`
  and `wal_compression=lz4` cut the fsyncs by about three quarters (see `zabbix-db.container`).

## License

MIT, see [LICENSE](LICENSE).
