# Public IPs and DNS

> **Experimental.** exo-v2v 0.6.0 is an early release, validated in one lab (ESXi 7.0 U3 to Exoscale ch-gva-2). Try it on non-critical VMs first; commands and behaviour may change.

A migrated VM gets a new public address on Exoscale. exo-v2v handles what
follows from that: an Elastic IP known before the window, the reverse DNS name
(mail servers), the DNS records that point at the service, and private
addresses that must change.

| Need | How |
|---|---|
| A public address known in advance, that outlives the instance | An **Elastic IP**, reserved at `create`, attached at cutover, configured in the guest |
| A PTR record (mail servers, some partners) | **Reverse DNS** on the Elastic IP or the instance, set at `create` |
| Names pointed at the new address at the right time | **DNS records** on Exoscale DNS: TTL lowered at `create`, switched once the instance is healthy, put back by `rollback` |
| A private address that cannot stay (the `.254` Exoscale keeps, a subnet that overlaps) | **Renumbering**: a new address per VM, or a whole network moved to a new range |
| Firewall rules that name other groups | Security group rules with `sg:NAME` sources |

## Elastic IPs

```bash
exo-v2v plan web01 --vm web01 --zone ch-gva-2 --elastic-ip new --reverse-dns www.example.com
```

In a group manifest:

```yaml
vms:
  - vm: lb01
    public: {elastic_ip: new, reverse_dns: shop.example.com}
  - vm: mail01
    public: {elastic_ip: 194.182.10.20}      # an Elastic IP you already have, in the same zone
```

| Step | What happens |
|---|---|
| `create` | `new` reserves an Elastic IP (description `v2v-<migration>`); an address you give must exist in the zone and be free. The address is printed: tell partners, update firewalls and DNS ahead |
| cutover | The Elastic IP is attached to the stopped instance before it starts. The conversion configures it in the guest (below). The cutover waits for the instance's SSH or RDP port on the Elastic IP too; in a group, a member whose Elastic IP does not answer fails its health check |
| `rollback` | The source VM runs again with its old address; the Elastic IP stays reserved |
| `cleanup` | The Elastic IP stays: it is the production address. `cleanup --delete-elastic-ips` (or `group cleanup --delete-elastic-ips`) releases the ones exo-v2v reserved; an address you brought is never released |

**In the guest.** Exoscale delivers an Elastic IP's traffic to the instance; the
guest must hold the address:

- **Linux:** `v2v-eip.service` puts every address of `/etc/v2v-eip.list` on the
  loopback interface at each boot (Exoscale's documented method). Replies leave
  from the Elastic IP; connections the guest opens still leave from the
  instance's own address.
- **Windows:** Windows adds no address to an interface on DHCP, so a first-boot
  task turns the public NIC static with what its DHCP lease gave (address,
  gateway, DNS servers: an Exoscale instance keeps its public address) and adds
  the Elastic IP as a /32 that is never used as a source. If a step fails, DHCP
  comes back and the task tries again. Log:
  `C:\ProgramData\exo-v2v\elastic-ip.log`.

The instance keeps its own public address as well (health checks use it), and
its security groups apply to the traffic of both addresses.

## Reverse DNS

`--reverse-dns NAME` (or `public.reverse_dns`) sets the PTR record of the
Elastic IP, or of the instance's address without one, at `create`. A mail
server can then send from its final address the day it moves. The forward
record (`NAME` → the address) is yours to create, or exo-v2v switches it (below).

## DNS records

For zones hosted on [Exoscale DNS](https://www.exoscale.com/dns/):

```bash
exo-v2v plan web01 --vm web01 --zone ch-gva-2 --dns-record www.example.com --dns-record example.com
```

```yaml
dns:
  ttl: 60                                   # until cleanup
  records:
    www.example.com: lb01                   # A record -> lb01's Elastic IP (or its instance IP)
    example.com: lb01
```

| Step | What happens |
|---|---|
| `plan` | Each name must be one A record in a domain of the account (the longest matching domain holds it) |
| `create` | The current value and TTL of each record are saved, the TTL lowered to `ttl`. **Wait at least the old TTL, plus two minutes, before the cutover**: resolvers keep the old answer that long (the cutover warns otherwise) |
| cutover | Once the instance answers (a group: once the last tier is healthy, before `post_cutover`), each record points at the new address: the Elastic IP, or the instance's address. Then the zone's nameservers are asked until every one of their addresses answers the new address 10 times in a row (at most 3 minutes; the cutover never fails on it, and a group's window does not include it). A cutover whose instance did not answer leaves DNS alone: run `exo-v2v cutover NAME` again once it does |
| `rollback` | The old values back (the TTL stays low) |
| `cleanup` | The original TTL back |

`status` and the web page show each record (old value, switched, how long
until the nameservers served it, restored).

Exoscale's nameservers serve a change after a few seconds to about a minute
(1 to 62 s in our tests). During that time each nameserver address can answer
the old and the new value in turn, so a single `dig` that shows the new address
does not mean the change is everywhere. The check runs from the appliance over
UDP port 53; where that is blocked it logs that it could not check, and carries on.
Only A records of Exoscale DNS zones are handled; for another DNS provider,
switch them in the group's `post_cutover` hook (`{lb01.address}` is the
Elastic IP or the instance address).

## Renumbering private addresses

A group keeps its VMs' private addresses by default. To change them:

```yaml
networks:
  app:
    source: pg-app
    type: private
    cidr: 10.20.0.0/24
    renumber: 10.30.0.0/24          # every member keeps its host part: 10.20.0.31 -> 10.30.0.31

vms:
  - vm: db01
    ip: {app: 10.20.0.50}           # or one VM: its new address on that network
```

`plan` prints `renumbered: OLD -> NEW`. At the conversion, the guest's network
configuration is rewritten in place wherever it names the old address
(ifupdown, netplan, NetworkManager, ifcfg, systemd-networkd, and the guest's own
line in `/etc/hosts`); Windows gets the new static address. **Addresses inside
applications** (a database address in a configuration file, another VM's line
in `/etc/hosts`) are not changed: renumber what nothing else names, or update
those with a hook. `renumber` needs the same prefix length (host parts are
kept); the `.254` of a /24 still belongs to Exoscale's DHCP server.

## Security group rules

The rules of a manifest's `security_groups` take a CIDR or another security
group as source:

```yaml
security_groups:
  v2v-shop-db: ["tcp/5432 sg:v2v-shop-app", "icmp 10.20.0.0/24"]
  v2v-shop-app: ["tcp/8080 sg:v2v-shop-web"]
```

`group create` creates the groups that do not exist, with their rules, and
keeps the rules of the groups it created in line with the manifest. For a
security group that existed before, `group plan` shows the rules that differ
and `group create --update-rules` adds the missing ones (it never removes rules
from a group exo-v2v did not create).

## Permissions

The [IAM role](iam.md) needs the Elastic IP operations (on `v2v-*` Elastic
IPs: an existing address you bring needs a description starting with `v2v-`,
or a rule of your own), the instance's reverse DNS, and, for DNS records, the
`dns` service rule. These rules are in the policy file since 0.6.0.
