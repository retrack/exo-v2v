# Web status page

> **Experimental.** exo-v2v 0.6.0 is an early release, validated in one lab (ESXi 7.0 U3 to Exoscale ch-gva-2). Try it on non-critical VMs first; commands and behaviour may change.

`exo-v2v web` serves a page with the state of every migration and group on the
replication appliance: phases, the last cycles, cutover steps, downtime, member
logs. Share it with the people who follow a migration without giving them a
shell. It is read-only unless you enable [actions](api.md) (a sync cycle, a
rehearsal, destroying a rehearsal; never a cutover or a rollback).

```bash
sudo -E exo-v2v web                      # http://127.0.0.1:8780/
```

Run it as the user that runs the migrations (`sudo -E`, the same
`EXO_V2V_HOME`): it reads their state files.

## What it shows

| Page | Content |
|---|---|
| Overview | Groups (cutover state, members by tier and phase), every migration: phase, a sync or cutover running now, a receiver down, failed cycles in a row (the last error on hover), last cycle (highlighted when older than the [lag alert](OPERATIONS.md#unattended-timers-and-alerts) threshold), public and private addresses, downtime; the [WAN limit](OPERATIONS.md#bandwidth-limit-schedule-compression) now; recent jobs |
| Migration | Phase, source VM, receiver, Elastic IP and DNS records, a chart of the data copied per cycle (full, delta, final), the bytes sent per cycle (after compression, with the time spent waiting for the WAN limit), disks and where they go, networks with the Exoscale MACs, the cutover steps with their times, plan and preflight messages, events, jobs |
| Job | The command, who started it, its state and log; a button to interrupt it |
| Rehearsal | Its copies (phase, address, seed and boot times, health from the relay), its steps, networks, the names the copies tried to resolve, the TTL |
| Group | Members by tier with their health check results, the cutover steps (preflight, warm cycle, tiers off, conversions, tiers up, hooks) with their times, networks, a member's log, events |

![The group page after a cutover: members by tier with their health checks, the cutover steps with their times, the networks](images/web-group.png)

The page refreshes every 5 seconds. It needs no internet access: no external
scripts, fonts or images.

## Read-only (by default)

- Reading never writes a file and never takes a lock: a running sync or cutover
  is found from the process ID its lock file records.
- Writes exist only for [actions](api.md): off unless `actions: enabled: true`
  in `config.yaml` **and** the server has a token (on loopback too). Any other
  write gets `405`.
- It needs no Exoscale or vSphere credentials, and serves no keys: the
  migration's SSH key and the tunnel's `known_hosts` are not exposed. Logs are
  served only for group members, by name.

## Access

It listens on `127.0.0.1:8780` by default. From your workstation:

```bash
ssh -L 8780:127.0.0.1:8780 appliance      # then open http://127.0.0.1:8780/
```

Without a token, it answers only requests addressed to `127.0.0.1` or
`localhost` (a web page elsewhere cannot reach it through a DNS name that
resolves to 127.0.0.1). To listen on another address, set a token; the page
refuses to start without one:

```bash
openssl rand -hex 24 | sudo tee /etc/exo-v2v-web.token >/dev/null
sudo -E exo-v2v web --listen 0.0.0.0:8780 --token-file /etc/exo-v2v-web.token
```

The page asks for the token once and keeps it in an HTTP-only cookie
(`/login?token=TOKEN` still works, but leaves the token in the browser's
history). API clients send `Authorization: Bearer TOKEN`. The page is plain HTTP:
put it behind a TLS reverse proxy, or keep the SSH tunnel, on any network you
do not trust. `EXO_V2V_WEB_TOKEN` can carry the token instead of a file.

## Run it as a service

```ini
# /etc/systemd/system/exo-v2v-web.service
[Unit]
Description=exo-v2v read-only status page
After=network-online.target

[Service]
Environment=EXO_V2V_HOME=/var/lib/exo-v2v
ExecStart=/opt/exo-v2v/bin/exo-v2v web --listen 127.0.0.1:8780
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## JSON API

| Path | Returns |
|---|---|
| `/api/overview` | Host, version, groups and migrations (summaries) |
| `/api/migrations/NAME` | One migration: plan, cycles, cutover steps, events |
| `/api/groups/NAME` | One group: members, cutover, networks, events |
| `/api/groups/NAME/log/VM?lines=N` | The last N lines (at most 2000) of a member's log |
| `/api/rehearsals/NAME` | One rehearsal: copies, relay, networks, steps, report |
| `/api/policy` | The actions this page may start |
| `/api/jobs[?target=NAME]`, `/api/jobs/ID`, `/api/jobs/ID/log` | Jobs ([actions](api.md)) |
