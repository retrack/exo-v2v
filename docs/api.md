# Actions, API and MCP

> **Experimental.** exo-v2v 0.6.0 is an early release, validated in one lab (ESXi 7.0 U3 to Exoscale ch-gva-2). Try it on non-critical VMs first; commands and behaviour may change.

The [web status page](web.md) can also start a few actions, through the same
HTTP API that scripts can call, and `exo-v2v mcp` lets an AI assistant read the
state of the migrations. Nothing here can cut over or roll back: those stay on
the command line, where someone types them.

| | Reads | Starts |
|---|---|---|
| Web page and its API | Everything the page shows | A sync cycle (migration) or round (group), a rehearsal, destroying a rehearsal: only once enabled, and only with a token |
| `exo-v2v mcp` | The same, as MCP tools | Nothing |
| Command line | Everything | Everything |

## Actions (jobs)

Off until you enable them in `$EXO_V2V_HOME/config.yaml`:

```yaml
actions:
  enabled: true
  web: [sync, rehearse, rehearsal-destroy]   # the default: drop what the page may not start
  max_running: 4
```

| Action | Runs | Options |
|---|---|---|
| `sync` | `exo-v2v sync NAME` (one cycle), or `exo-v2v group sync GROUP` (one round) | group: `consistent`, `quiesce` |
| `rehearse` | `exo-v2v rehearse NAME` or `exo-v2v group rehearse GROUP` (settings from the manifest) | `ttl`; group: `consistent`, `quiesce` |
| `rehearsal-destroy` | `exo-v2v rehearsal destroy NAME` | |

Each action becomes a **job**: its own `exo-v2v` process, recorded in
`$EXO_V2V_HOME/jobs/ID/` (`job.json`, `log`). On the appliance (root with
systemd) a job is a transient unit `exo-v2v-job-ID` that reads the credentials
of `/etc/exo-v2v/exo-v2v.env` (written by `exo-v2v service install
--write-env`) and outlives the web server. A target that is busy (a sync, a
cutover, another job) is refused, never queued. Jobs send the `job-started`,
`job-finished` and `job-failed` [alerts](OPERATIONS.md#unattended-timers-and-alerts).

```bash
exo-v2v job list [--target NAME]       # recent jobs: state, who started them, error
exo-v2v job log ID                     # its output
exo-v2v job cancel ID                  # SIGINT: a sync finishes the cycle or round in progress, then stops
exo-v2v job start sync group shop      # what the page does, from a script
```

On the page, each migration, group and rehearsal shows its buttons; a click
shows the exact command and asks to confirm (destroying a rehearsal asks for
its name). The job's page follows its log.

## HTTP API

Read endpoints: see [Web status page](web.md#json-api). Writes:

| Request | Body | Answer |
|---|---|---|
| `POST /api/jobs` | `{"action": "sync", "kind": "group", "target": "shop", "params": {"consistent": true}}` | `202` and the job; `400` bad request, `403` not enabled, `404` no such target, `409` busy |
| `POST /api/jobs/ID/cancel` | `{}` | the job (an interrupted sync finishes its cycle or round: nothing is left half-copied; a rehearsal stops where it is, `rehearse` again resumes it, `rehearsal destroy` removes it) |
| `GET /api/jobs[?target=NAME]`, `GET /api/jobs/ID`, `GET /api/jobs/ID/log?lines=N` | | jobs, one job, its log |
| `GET /api/policy` | | the actions this page may start |

Every write needs all of:

- a token on the server (`--token-file` or `EXO_V2V_WEB_TOKEN`), **on loopback
  too**, sent as `Authorization: Bearer TOKEN` (or the page's cookie);
- `Content-Type: application/json` and the header `X-Exo-V2V: 1`;
- no cross-site origin (`Origin`, `Sec-Fetch-Site`).

```bash
curl -s -X POST http://127.0.0.1:8780/api/jobs -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -H "X-Exo-V2V: 1" \
  -d '{"action": "sync", "kind": "migration", "target": "web01"}'
```

## MCP server

`exo-v2v mcp` speaks the [Model Context Protocol](https://modelcontextprotocol.io/)
over stdin and stdout (JSON-RPC 2.0, one message per line). It is read-only: its
tools return what the web API returns, and none changes anything.

| Tool | Returns |
|---|---|
| `overview` | Every group, migration and rehearsal: phase, last cycle, failures, receivers down, WAN limit, recent jobs |
| `migration` | One migration: plan (disks, networks, public side), cycles with the bytes sent, cutover steps, events |
| `group` | One group: members by tier, sync rounds, cutover and window, DNS records, rehearsal, events |
| `group_member_log` | The tail of a member's log |
| `rehearsal` | A rehearsal and its report: health, predicted window, consistent point, names the copies tried to resolve |
| `jobs`, `job` | Jobs started from the page, one job with its log |
| `wan_status` | The bandwidth limit now, the next scheduled change, compression |

Run it on the appliance through SSH, as the user that runs the migrations. Allow
exactly that command without a password:

```text
# /etc/sudoers.d/exo-v2v-mcp
ops ALL=(root) NOPASSWD: /usr/local/bin/exo-v2v mcp
```

Then point your MCP client at it, for example:

```json
{
  "mcpServers": {
    "exo-v2v": {"command": "ssh", "args": ["ops@appliance", "sudo", "-n", "/usr/local/bin/exo-v2v", "mcp"]}
  }
}
```

It reads `EXO_V2V_HOME` from `/etc/exo-v2v/exo-v2v.env` when the environment
has none (sudo resets it). It needs no vSphere or Exoscale credentials.
