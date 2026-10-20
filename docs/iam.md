# IAM role for the replication appliance

> **Experimental.** exo-v2v 0.1.0 is an early release, validated in one lab (ESXi 7.0 U3 to Exoscale ch-gva-2). Try it on non-critical VMs first; commands and behaviour may change.

The replication appliance calls the Exoscale API through the `exo` CLI. Give it
an API key bound to a dedicated role, not your account's own key.

exo-v2v only uses the Compute service: instances, Block Storage volumes and
security groups. Everything it creates follows fixed naming rules, which the
role below uses to limit what the key can touch.

| Resource | Name |
|---|---|
| Target instance | `v2v-<migration>` |
| Block Storage volumes | `v2v-<migration>-d<N>`, `v2v-<migration>-d<N>-4k` |
| Security group | `exo-v2v-<migration>` |
| Temporary SSH key (registered by `exo` while it creates the instance, then deleted) | `v2v-<migration>-<timestamp>` |

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
| 2 | Creating `v2v-*` instances, volumes and SSH keys |
| 3 | Deleting `v2v-*` SSH keys |
| 4 | Starting, stopping, scaling and deleting `v2v-*` instances |
| 5 | Attaching a `v2v-*` volume to a `v2v-*` instance |
| 6 | Detaching, relabelling and deleting `v2v-*` volumes |
| 7 | Creating `exo-v2v-*` security groups |
| 8 | Adding rules to, deleting, and detaching instances from `exo-v2v-*` security groups |
| 9 | Attaching an instance to a security group (cutover attaches the groups you name in `plan --security-group`) |

Everything else is denied, including every other Exoscale service. The
operations in the rules are the ones exo-v2v 0.1.0 calls through `exo` 1.103:
recorded during a full migration (create, sync, cutover with a volume rebuild,
cleanup), plus the security-group attach and detach used by
`plan --security-group` and by `cleanup` without `--delete-instance`.

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
and AlmaLinux 9 with a volume rebuild) through `cleanup --delete-instance`, with
every `exo` call made with the role's key. Rule 9 and the security-group detach
in rule 8 were not exercised: those tests used neither `plan --security-group`
nor `cleanup` without `--delete-instance`.

Run one migration of a small test VM with your new key too, through `cleanup
--delete-instance`. A refused call stops exo-v2v with an error that names the
`exo` command; check the Exoscale console for leftovers after a failed test.

## What the role does not restrict

- Read operations cover every Compute resource of the organization, not only
  exo-v2v's: the `exo` CLI looks resources up by name.
- Rule 9 lets the key attach any security group to any instance. Exoscale
  evaluates that operation against the security group, not the instance.
- Every rule matches names (`v2v-`, `exo-v2v-`), not labels. Do not give
  other instances, volumes, SSH keys or security groups names with those
  prefixes, or the key can manage them too.

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
