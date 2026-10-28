# IAM role for the replication appliance

> **Experimental.** exo-v2v 0.6.0 is an early release, validated in one lab (ESXi 7.0 U3 to Exoscale ch-gva-2). Try it on non-critical VMs first; commands and behaviour may change.

The replication appliance calls the Exoscale API through the `exo` CLI. Give it
an API key bound to a dedicated role, not your account's own key.

exo-v2v only uses the Compute service: instances, Block Storage volumes,
security groups and, for groups, private networks and anti-affinity groups.
Everything it creates follows fixed naming rules, which the role below uses to
limit what the key can touch.

| Resource | Name |
|---|---|
| Target instance | `v2v-<migration>` (group members: `v2v-<group>-<vm>`) |
| Block Storage volumes | `v2v-<migration>-d<N>`, `v2v-<migration>-d<N>-4k` |
| Security group of a migration | `exo-v2v-<migration>` |
| Temporary SSH key (registered by `exo` while it creates the instance, then deleted) | `v2v-<migration>-<timestamp>` |
| Group private network | `v2v-<group>-<network>` (or a `v2v-*` network you name with `exoscale_name`) |
| Group anti-affinity group | `v2v-<group>-<name>` |
| Security groups a group manifest defines | `v2v-*` (or `exo-v2v-*`) |
| Rehearsal: copies, relay, networks, its security group | `v2v-<migration>-rh`, `v2v-<name>-rh-relay`, `v2v-<name>-rh-<network>` |
| Rehearsal: snapshots and volumes | `v2v-<volume>-rh-<time>`, `v2v-<migration>-rh-d<N>` |

If you name the instance yourself with `create --instance-name`, start the name
with `v2v-`, or the role below refuses it.

exo-v2v also labels its instances and volumes `exo-v2v=<migration>`, but the
role does not rely on labels: in our tests, Exoscale IAM refused
`create-instance` and `create-block-storage-volume` calls under a rule that
checked `parameters.labels`, while rules on names worked.

## The role

[`iam/exo-v2v-appliance-policy.json`](iam/exo-v2v-appliance-policy.json):

| Rule | Allows |
|---|---|
| 1 | Every read operation (`get-*`, `list-*`) in Compute: zones, templates, instance types, and the state of the resources above |
| 2 | Creating `v2v-*` instances (with or without a public IP), volumes (also from a snapshot), SSH keys, private networks and anti-affinity groups |
| 3 | Deleting `v2v-*` SSH keys |
| 4 | Starting, stopping, scaling, resetting, renaming (`update-instance`, used by `cleanup` for a group member's final name) and deleting `v2v-*` instances. Reset: when the source system disk grows during replication, the target is re-imaged with a bigger root disk |
| 5 | Attaching a `v2v-*` volume to a `v2v-*` instance |
| 6 | Detaching, relabelling, resizing and deleting `v2v-*` volumes |
| 7 | Snapshotting volumes into `v2v-*` snapshots (rehearsal seeding) |
| 8 | Deleting and relabelling `v2v-*` snapshots |
| 9 | Attaching instances to `v2v-*` private networks (with a static lease) |
| 10 | Deleting `v2v-*` private networks |
| 11 | Deleting `v2v-*` anti-affinity groups |
| 12 | Creating `exo-v2v-*` and `v2v-*` security groups |
| 13 | Adding and deleting rules, deleting, and detaching instances from `exo-v2v-*` and `v2v-*` security groups |
| 14 | Attaching an instance to a security group (cutover attaches the groups you name in `plan --security-group` or in a group manifest) |
| 15 | Creating Elastic IPs whose description starts with `v2v-` (`--elastic-ip new`) |
| 16 | Attaching, detaching, setting the reverse DNS of, and deleting `v2v-*` Elastic IPs (by description: an Elastic IP you bring needs a description starting with `v2v-`, or a rule of your own) |
| 17 | Setting the reverse DNS of a `v2v-*` instance |
| 18 | DNS: reading domains and records, updating records (the [DNS switch](public.md#dns-records)); no domain or record is created or deleted. Narrow it to your domains, or drop it if you do not switch DNS with exo-v2v |

Everything else is denied, including every other Exoscale service. The
operations in the rules are the ones exo-v2v calls through `exo` 1.103:
recorded during a full migration with 0.1.0 (create, sync, cutover with a
volume rebuild, cleanup), plus the security-group attach and detach used by
`plan --security-group` and by `cleanup` without `--delete-instance`. 0.2.0
adds `resize-block-storage-volume` (a data disk that grows during replication)
and `reset-instance` (a system disk that grows). 0.3.0 adds what groups need:
`create-private-network`, `attach-instance-to-private-network`,
`delete-private-network`, `create-anti-affinity-group`,
`delete-anti-affinity-group`, `update-instance` (rename), and `v2v-*` security
groups. Exoscale evaluates `attach-instance-to-private-network` against the
private network, not the instance: a rule on the instance name never matches it. 0.4.0 adds
`create-block-storage-snapshot` (matched on the snapshot's name: the volume is not loaded for
that operation) and `delete-block-storage-snapshot` for rehearsals. 0.4.1 adds
`delete-rule-from-security-group`: a receiver's security group allows no egress until the
cutover, which removes that rule before it stops the source (without the permission the cutover
stops there, with nothing stopped).

0.6.0 adds rules 15 to 18 (Elastic IPs, reverse DNS, DNS records). They follow
the operation names of the Exoscale API but were not yet exercised under a
restricted key in our lab (the lab ran them with an unrestricted one): if a
call is refused, the command stops with `Forbidden` before anything else
changes, and the rule to widen is the one above.

To update an existing role: `exo iam role update exo-v2v-appliance --policy - < docs/iam/exo-v2v-appliance-policy.json`.

Create the role and a key bound to it with the `exo` CLI, from an account that
can manage IAM:

```bash
exo iam role create exo-v2v-appliance \
  --description "exo-v2v replication appliance" \
  --policy - < docs/iam/exo-v2v-appliance-policy.json
exo iam api-key create exo-v2v-appliance exo-v2v-appliance
```

The second command prints the key and its secret once. On the appliance, store
them with `exo config` or export them as `EXOSCALE_API_KEY` and
`EXOSCALE_API_SECRET` for the user that runs exo-v2v.

## Check the role before production

We validated this role on 2026-10-19 with two complete migrations (Ubuntu 24.04,
and AlmaLinux 9 with a volume rebuild) through `cleanup --delete-instance`, on
2026-10-21 with a data disk and a system disk that grew during replication,
and on 2026-10-23 with three group runs (5 VMs): `group create` (private
network, anti-affinity group, security groups, receivers attached to the
private network with static leases), the cutovers, `group rollback`,
`group cleanup --delete-instances`, and a hand-over `group cleanup` with a
rename. On 2026-10-23 also with rehearsals (snapshots, volumes from snapshots, instances without a
public IP, the relay and its networks, destroy). Every `exo` call was made with the role's key.

Run one migration of a small test VM with your new key too, through `cleanup
--delete-instance`. A refused call stops exo-v2v with an error that names the
`exo` command; check the Exoscale console for leftovers after a failed test.

## What the role does not restrict

- Read operations cover every Compute resource of the organization, not only
  exo-v2v's: the `exo` CLI looks resources up by name.
- Rule 9 lets the key attach any security group to any instance. Exoscale
  evaluates that operation against the security group, not the instance.
- Every rule matches names (`v2v-`, `exo-v2v-`), not labels. Do not give
  other instances, volumes, SSH keys, private networks, anti-affinity groups or
  security groups names with those prefixes, or the key can manage them too.
- Rule 7 lets the key attach any `v2v-*` instance to a `v2v-*` private network.
- A group member renamed at `cleanup` leaves the role's scope: that is the
  hand-over (the key can no longer stop or delete it).

## A simpler alternative

If you prefer a role that is easier to read, allow the whole Compute service
and nothing else:

```json
{
  "default-service-strategy": "deny",
  "services": { "compute": { "type": "allow", "rules": [] } }
}
```

It can manage every Compute resource of the organization, so use it only in
an organization dedicated to the migration.
