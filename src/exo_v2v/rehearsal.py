"""Rehearsal: copies of the migrated VMs started in an airgap, reachable only
through a relay, while the sources run and replication goes on.

  exo-v2v rehearse NAME [--access CIDR] [--ssh-key FILE] [--ttl 8h] [--tcp PORT | --http URL]
  exo-v2v group rehearse GROUP [--vm VM ...]        # settings: the manifest's `rehearsal:`
  exo-v2v rehearsal status|connect|report|destroy NAME
  exo-v2v gc                                        # destroys rehearsals past their TTL

Per member, a copy (`v2v-<migration>-rh`) boots the exo-v2v receiver template
without a public IP, on isolated copies of the networks plus a front network
that stands in for the public interface (same NICs, same order as after the
real cutover). It is seeded from the production replica: Block Storage
snapshots become its volumes, and the production receiver streams its root disk
to it through the relay while the migration's lock is held. The cutover's own
conversion runs on the copy, then the copies start tier by tier, the health
checks running on the relay. Nothing behind the relay has a route out: the
relay accepts SSH only (from the appliance and the allowed addresses), does not
forward, and answers DNS for listed names only, logging every query.
State: ~/.exo-v2v/rehearsals/NAME/rehearsal.json; each copy is a migration
state named <migration>-rh.
"""
import argparse
import base64
import concurrent.futures as cf
import copy
import io
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time

from . import userdata
from .exo import Exo, ExoError, volume_size_gb
from .state import State, home
from .tunnel import CIPHERS, Tunnel
from .util import die, human, log, log_prefix, write_json_atomic

LABEL = "exo-v2v"
RH_LABEL = "exo-v2v-rehearsal"
RELAY_TEMPLATE = "Linux Debian 13 (Trixie) 64-bit"
RELAY_TYPE = "standard.small"
DEFAULTS = {"access": [], "ssh_keys": [], "front_cidr": "100.127.0.0/24", "hosts": {}, "ttl": "8h"}


class RehearsalFailure(Exception):
    pass


def rdir():
    return os.path.join(home(), "rehearsals")


class Rehearsal:
    lk = threading.RLock()            # copies are worked on in threads: one writer at a time

    def __init__(self, name):
        self.name = name
        self.dir = os.path.join(rdir(), name)
        self.path = os.path.join(self.dir, "rehearsal.json")
        self.data = {}

    @classmethod
    def load(cls, name, must=True):
        r = cls(name)
        if os.path.exists(r.path):
            r.data = json.load(open(r.path))
        elif must:
            die(f"no rehearsal {name!r} (looked in {r.path})")
        return r

    @staticmethod
    def all():
        d = rdir()
        return sorted(x for x in os.listdir(d) if os.path.exists(os.path.join(d, x, "rehearsal.json"))) \
            if os.path.isdir(d) else []

    @property
    def active(self):
        return self.data.get("state") not in (None, "destroyed")

    def save(self):
        with Rehearsal.lk:
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            write_json_atomic(self.path, self.data)

    def event(self, msg):
        with Rehearsal.lk:
            self.data.setdefault("events", []).append({"t": time.time(), "msg": msg})
            self.save()
        log(msg)

    def mark(self, step):
        self.data["steps"][step] = time.time()
        self.data["step"] = step
        self.save()

    def lock(self, what):
        import fcntl
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        lk = open(os.path.join(self.dir, ".lock"), "a+")
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            die(f"another exo-v2v command on rehearsal {self.name} is running")
        lk.seek(0); lk.truncate()
        lk.write(json.dumps({"pid": os.getpid(), "what": what, "since": time.time()}))
        lk.flush()
        self._lock = lk

    @property
    def key_path(self):
        return os.path.join(self.dir, "relay_key")

    def relay_tunnel(self):
        r = self.data["relay"]
        return Tunnel(r["ip"], "debian", self.key_path, os.path.join(self.dir, "known_hosts"))

    def jump(self):
        return {"host": self.data["relay"]["ip"], "user": "debian", "key": self.key_path,
                "known_hosts": os.path.join(self.dir, "known_hosts")}

    def relay(self, cmd, input=None, timeout=600, check=True):
        p = self.relay_tunnel().ssh(cmd, input=input, timeout=timeout)
        if check and p.returncode:
            raise RehearsalFailure(f"relay: `{cmd[:80]}` rc={p.returncode}: {(p.stderr or p.stdout).strip()[-300:]}")
        return p


def active_for(migration):
    """The active rehearsal that copies `migration`, or None."""
    for n in Rehearsal.all():
        r = Rehearsal.load(n)
        if r.active and any(m["migration"] == migration for m in r.data.get("members", [])):
            return n
    return None


def shadow_name(migration):
    return f"{migration}-rh"


def shadow_template(p):
    """The copy boots a receiver template (a copy cannot install packages: no internet)."""
    if p.get("receiver_mode") == "template":
        return p["template"], p.get("template_visibility", "public")
    fam = "windows" if p["os_family"] == "windows" else "linux"
    return f"exo-v2v-{fam}-{'uefi' if p['firmware'] == 'efi' else 'legacy'}", "private"


def duration(v):
    from .group import duration as d
    return d(v)


def parallel(fn, items, workers=8):
    """Run fn(item) in threads; {item key: None | error text}. die() in a thread is caught."""
    def run(x):
        with log_prefix(x["vm"]):
            try:
                fn(x)
                return None
            except SystemExit as e:
                return str(e.code) if not isinstance(e.code, int) else "failed (see the log above)"
            except (RehearsalFailure, ExoError, OSError, subprocess.SubprocessError) as e:
                log(f"error: {e}")
                return str(e)
    with cf.ThreadPoolExecutor(max(1, min(workers, len(items)))) as pool:
        return dict(zip([x["vm"] for x in items], pool.map(run, items)))


def wait_lock(st, what, timeout=3600):
    """The migration's lock (no sync cycle meanwhile), waiting for a running one."""
    import fcntl
    lk = open(os.path.join(st.dir, ".lock"), "a+")
    t0 = time.time()
    while True:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() - t0 > timeout:
                raise RehearsalFailure(f"{st.name}: its sync did not finish in {timeout}s")
            time.sleep(10)
    lk.seek(0); lk.truncate()
    lk.write(json.dumps({"pid": os.getpid(), "what": what, "since": time.time()}))
    lk.flush()
    return lk


# ---------------- layout ----------------

def net_cidr(ex, pn):
    d = ex.privnet(pn)
    return str(ipaddress.ip_network(f"{d['start_ip']}/{d['netmask']}", strict=False))


def layout(rh, sts, ex):
    """The rehearsal networks: the front network, then a copy of every private
    network the members use, each with the relay's address (the highest free
    address of the range) and the DNS addresses the guests have configured there."""
    n, s = rh.name, rh.data["settings"]
    nets = [{"name": f"v2v-{n}-rh-front", "role": "front", "source": "public", "cidr": s["front_cidr"]}]
    for st in sts.values():
        for x in st["plan"].get("networks", []):
            if x["network"] != "public" and x["network"] not in [y["source"] for y in nets]:
                short = x["network"][len(f"v2v-{st['plan'].get('group') or ''}-"):] \
                    if st["plan"].get("group") and x["network"].startswith(f"v2v-{st['plan']['group']}-") \
                    else re.sub(r"^v2v-", "", x["network"])
                nets.append({"name": f"v2v-{n}-rh-{short}"[:60], "role": "private", "source": x["network"],
                             "cidr": net_cidr(ex, x["network"])})
    front = ipaddress.ip_network(s["front_cidr"], strict=False)
    for x in nets[1:]:
        if ipaddress.ip_network(x["cidr"]).overlaps(front):
            die(f"rehearsal front network {front} overlaps {x['source']} ({x['cidr']}): set front_cidr")
    dns = {a for st in sts.values() for nic in (st["plan"].get("inventory") or {}).get("nics", [])
           for a in nic.get("dns", []) if ":" not in a}
    for x in nets:
        net = ipaddress.ip_network(x["cidr"])
        used = {nn["ip"] for st in sts.values() for nn in st["plan"].get("networks", [])
                if nn.get("ip") and nn["network"] == x["source"]}
        a = net.broadcast_address - 2                    # the top address belongs to Exoscale's DHCP
        while str(a) in used and a > net.network_address + 1:
            a -= 1
        x["relay_ip"] = str(a)
        x["prefix"] = net.prefixlen
        x["aliases"] = sorted(d for d in dns if ipaddress.ip_address(d) in net and d not in used
                              and d != x["relay_ip"]) if x["role"] == "private" else []
    return nets


# ---------------- the relay ----------------

def relay_userdata(pub, user_keys):
    keys = "".join(f"      - {k.strip()}\n" for k in user_keys) or "      []\n"
    return f"""#cloud-config
ssh_authorized_keys:
  - {pub.strip()}
users:
  - default
  - name: rehearsal
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
{keys}write_files:
  - path: /etc/sysctl.d/90-v2v-relay.conf
    content: |
      net.ipv4.ip_forward = 0
      net.ipv6.conf.all.forwarding = 0
  - path: /etc/nftables.conf
    content: |
      #!/usr/sbin/nft -f
      flush ruleset
      table inet v2v_relay {{
        chain forward {{ type filter hook forward priority 0; policy drop; }}
      }}
  - path: /etc/ssh/sshd_config.d/90-v2v-relay.conf
    content: |
      PasswordAuthentication no
      KbdInteractiveAuthentication no
      PermitRootLogin no
      AllowAgentForwarding no
      AllowStreamLocalForwarding no
      X11Forwarding no
      PermitTunnel no
      GatewayPorts no
      AllowTcpForwarding local
  - path: /etc/ssh/sshd_config.d/91-v2v-permitopen.conf
    content: |
      PermitOpen none
  - path: /etc/dnsmasq.d/v2v-relay.conf
    content: |
      bind-interfaces
      listen-address=127.0.0.1
      no-resolv
package_update: true
packages: [dnsmasq, nftables, netcat-openbsd, curl]
runcmd:
  - [sysctl, --system]
  - [systemctl, enable, --now, nftables]
  - [systemctl, restart, ssh]
  - [touch, /var/lib/v2v-relay-ready]
"""


def relay_files(rh, ex):
    """networkd files (one per rehearsal NIC, by MAC: static addresses, no
    routes), the DNS sinkhole, PermitOpen."""
    ids = {ex.privnet(x["name"])["id"]: x for x in rh.data["networks"]}
    files = {}
    for nic in ex.instance_nics(rh.data["relay"]["name"]):
        x = ids.get(nic["network"])
        if not x:
            continue
        addrs = "".join(f"Address={a}/{x['prefix']}\n" for a in [x["relay_ip"]] + x["aliases"])
        files[f"/etc/systemd/network/20-v2v-{x['role']}-{nic['mac'].replace(':', '')}.network"] = (
            f"[Match]\nMACAddress={nic['mac']}\n[Network]\n{addrs}LinkLocalAddressing=no\nIPv6AcceptRA=no\n")
    listen = [a for x in rh.data["networks"] for a in [x["relay_ip"]] + x["aliases"]]
    hosts = "".join(f"address=/{k}/{v}\n" for k, v in sorted(rh.data["settings"]["hosts"].items()))
    files["/etc/dnsmasq.d/v2v-relay.conf"] = (
        "# exo-v2v rehearsal: answers the listed names only, refuses the rest at once, logs every query\n"
        "bind-interfaces\n" + "".join(f"listen-address={a}\n" for a in listen) +
        "no-resolv\nno-hosts\ndomain-needed\nbogus-priv\nlog-queries\nlog-facility=/var/log/v2v-relay-dns.log\n" + hosts)
    return files


def relay_permit(rh):
    """PermitOpen: the copies' addresses only (ssh -L / -W to anything else is refused)."""
    ips = sorted(set(rh.data.get("permit", [])))
    line = "PermitOpen " + (" ".join(f"{a}:*" for a in ips) if ips else "none")
    rh.relay(f"echo {shlex.quote(line)} | sudo tee /etc/ssh/sshd_config.d/91-v2v-permitopen.conf >/dev/null "
             "&& sudo systemctl reload ssh")


def permit(rh, *ips):
    with Rehearsal.lk:                 # one sshd config writer: a stale list would lock a copy out
        p = set(rh.data.setdefault("permit", []))
        if not set(ips) <= p:
            rh.data["permit"] = sorted(p | set(ips))
            rh.save()
            relay_permit(rh)


def create_relay(rh, ex, sender, timeout):
    n, s = rh.name, rh.data["settings"]
    name, sg = f"v2v-{n}-rh-relay", f"v2v-{n}-rh-relay"
    rh.data["relay"] = {"name": name, "sg": sg}
    if not os.path.exists(rh.key_path):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"exo-v2v-rehearsal-{n}",
                        "-f", rh.key_path], check=True)
    if not ex.exists("sg", sg):
        ex.sg_create(sg)
        for cidr in [sender] + list(s["access"]):
            ex.sg_rule_add(sg, cidr, 22, desc="exo-v2v rehearsal relay (SSH)")
    if not ex.exists("instance", name):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(relay_userdata(open(rh.key_path + ".pub").read(), s["ssh_keys"]))
            ud = f.name
        try:
            inst = ex.instance_create(name, RELAY_TEMPLATE, RELAY_TYPE, 10, [sg], ud,
                                      {LABEL: f"{n}-relay", RH_LABEL: n})
        finally:
            os.unlink(ud)
        rh.data["relay"]["ip"] = inst.get("ip_address")
        rh.save()
    else:
        rh.data["relay"]["ip"] = ex.instance(name).get("ip_address")
    attached = set(ex.instance(name).get("private_networks") or [])
    for x in rh.data["networks"]:
        if x["name"] not in attached:
            ex.privnet_attach(name, x["name"], x["relay_ip"])
    t0 = time.time()
    while rh.relay_tunnel().ssh("test -e /var/lib/v2v-relay-ready", timeout=30).returncode:
        if time.time() - t0 > timeout:
            raise RehearsalFailure("the relay did not finish its setup (cloud-init)")
        time.sleep(10)
    script = "set -e\n"
    for path, body in relay_files(rh, ex).items():
        script += f"cat > {path} <<'V2V_EOF'\n{body}V2V_EOF\n"
    script += ("systemctl restart systemd-networkd\nsystemctl enable dnsmasq >/dev/null 2>&1\n"
               "for i in $(seq 1 15); do sleep 2; systemctl restart dnsmasq && break; done\n"   # addresses must be up
               "networkctl --no-pager list | tail -n +2\n")
    p = rh.relay("sudo bash -s", input=script)
    log(f"relay {name} ({rh.data['relay']['ip']}) ready: {', '.join(x['name'] + ' ' + x['relay_ip'] for x in rh.data['networks'])}")
    relay_permit(rh)
    return p


# ---------------- copies ----------------

def make_shadow(rh, x, st, ex, timeout):
    """The copy's receiver: no public IP, front network at creation, the
    private copies attached with the members' addresses (static leases)."""
    from .cli import attach_networks, wait_ssh, tunnel_for
    sname = shadow_name(st.name)
    nets = {y["source"]: y["name"] for y in rh.data["networks"]}
    front = nets["public"]
    if os.path.exists(os.path.join(home(), sname, "state.json")):
        sh = State.load(sname)
    else:
        p = copy.deepcopy(st["plan"])
        tpl, vis = shadow_template(p)
        p.update(template=tpl, template_visibility=vis, receiver_mode="template", receiver_user="v2v",
                 security_groups=[], anti_affinity_groups=[], errors=[], warnings=[], notes=[],
                 networks=[{**y, "network": nets[y["network"]], **({"name": "front"} if y["network"] == "public" else {})}
                           for y in p.get("networks") or [{"network": "public", "mode": "dhcp"}]])
        sh = State.new(sname, p)
        sh.data.update(rehearsal=rh.name, rehearsal_of=st.name)
        sh.save()
    p, r = sh["plan"], sh["receiver"]
    if not os.path.exists(sh.key_path):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"exo-v2v-{sname}",
                        "-f", sh.key_path], check=True)
    name = f"v2v-{sname}"
    r.update(name=name, jump=rh.jump())
    r.setdefault("volumes", [])
    if not ex.exists("instance", name):
        sh.set_phase("receiver-creating")
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(userdata.build(open(sh.key_path + ".pub").read()))
            ud = f.name
        try:
            log(f"creating the copy's receiver {name} ({p['template']}, no public IP)")
            inst = ex.instance_create(name, p["template"], p["convert_type"], p["root_gb"], [], ud,
                                      {LABEL: sname, RH_LABEL: rh.name}, secureboot=p.get("secure_boot", False),
                                      tpm=p.get("tpm", False), template_visibility=p["template_visibility"],
                                      public_ip="none", private_networks=[front])
        finally:
            os.unlink(ud)
        r["id"] = inst.get("id")
    for _ in range(30):
        r["ip"] = ex.privnet_leases(front).get(name)
        if r["ip"]:
            break
        time.sleep(5)
    sh.save()
    permit(rh, r["ip"], *[y["ip"] for y in p["networks"] if y.get("ip")])
    wait_ssh(tunnel_for(sh), timeout)
    ids = {ex.privnet(y["network"])["id"]: y["network"] for y in p["networks"]}
    r["nics"] = [{**z, "network": ids.get(z["network"], z["network"])} for z in ex.instance_nics(name)]
    sh.save()
    attach_networks(sh, ex)
    log(f"receiver up at {r['ip']} (through the relay), NICs: "
        + ", ".join(f"{z['network']} {z['mac']}" for z in r["nics"]))
    return sh


def seed_egress_desc(rh):
    return f"exo-v2v: rehearsal {rh.name} relay"


def seed_egress(rh, sts, ex):
    """Production receivers whose egress is closed (exo-v2v >= 0.4.1) may open
    SSH to the relay for the seed: the root disk stream goes that way. Exoscale
    applies the rule within a minute or two; the stream retries until then."""
    done = rh.data.setdefault("seed_egress", [])
    for st in sts.values():
        r = st["receiver"]
        if st.name in done or not r.get("egress_closed") or not r.get("sg"):
            continue
        if not any(x.get("description") == seed_egress_desc(rh) for x in ex.sg_egress_rules(r["sg"])):
            ex.sg_rule_add(r["sg"], f"{rh.data['relay']['ip']}/32", 22, desc=seed_egress_desc(rh), flow="egress")
        done.append(st.name)
        rh.save()


def stream_root(rh, st, sh, port):
    """The production receiver copies its root disk (the replica) onto the
    copy's receiver: NBD through an SSH tunnel via the relay."""
    from .cli import tunnel_for
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "receiver")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arc, path in (("relay_key", rh.key_path), ("shadow_key", sh.key_path),
                          ("rootcopy.py", os.path.join(here, "rootcopy.py")),
                          ("bootmask.py", os.path.join(here, "bootmask.py")),
                          ("nbdproto.py", os.path.join(here, "nbdproto.py"))):
            tar.add(path, arcname=arc)
    # read through the production receiver's boot mask (exo-v2v >= 0.4.1): the copy gets
    # the real boot sectors; without a mask (an older agent) rootcopy reads the disk as it is
    root = next(iter(userdata.boot_mask(st["plan"])), "sda")
    b64 = base64.b64encode(buf.getvalue()).decode()
    d = f"/run/v2v-rh-{sh.name}"
    o = (f"-o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
         f"-o LogLevel=ERROR -o BatchMode=yes -o Ciphers={CIPHERS}")
    fwd = f"127.0.0.1:{port}:127.0.0.1:10809"
    script = f"""set -e
D={d}; rm -rf $D; mkdir -p -m 700 $D
base64 -d > $D/p.tgz <<'V2V_B64'
{b64}
V2V_B64
tar -xzf $D/p.tgz -C $D; chmod 600 $D/relay_key $D/shadow_key
trap 'pkill -f "{fwd}" || true; rm -rf $D' EXIT
# the egress rule towards the relay may take a minute or two to apply (Exoscale)
for i in $(seq 1 16); do
  ssh -i $D/shadow_key {o} -o ConnectTimeout=15 -o ExitOnForwardFailure=yes -o "ProxyCommand=ssh -i $D/relay_key {o} -o ConnectTimeout=15 -W %h:%p debian@{rh.data['relay']['ip']}" -f -N -L {fwd} v2v@{sh['receiver']['ip']} && break
  [ $i = 16 ] && exit 1; sleep 10
done
python3 $D/rootcopy.py /dev/vda 127.0.0.1:{port} sda 4 --overlay {root} --mask-config /opt/v2v-agent/mask.json
"""
    t = tunnel_for(st)
    p = t.ssh("sudo bash -s", input=script, timeout=12 * 3600)
    last = (p.stdout.strip().splitlines() or ["{}"])[-1]
    try:
        res = json.loads(last)
    except ValueError:
        res = {}
    if p.returncode or res.get("errors"):
        raise RehearsalFailure(f"root disk copy failed: {res.get('errors') or (p.stderr or p.stdout).strip()[-400:]}")
    return res


def consistent_point(rh, members, consistent, accept=False):
    """Before seeding: every member's replica at one point (see group.consistent_cycle).
    A resumed rehearsal redoes it while nothing is seeded; once some copies are, the
    others must not have moved past it."""
    cons = rh.data.get("consistency")
    seeded = [m for m in members if State.load(shadow_name(m["migration"])).data.get("seeded")]
    if cons is None or not seeded:
        rec, locks = consistent()
        rh.data["consistency"] = rec
        rh.save()
        rh.event(f"consistent point {rec['snapshot']}: spread {rec['spread_s']}s, {rec['mode']}")
        return locks
    moved = [m["vm"] for m in members if m not in seeded and
             ((State.load(m["migration"])["cycles"] or [{}])[-1].get("snapshot_name") != cons["snapshot"])]
    if moved and not accept:
        raise RehearsalFailure(f"{', '.join(moved)} replicated past the consistent point since seeding began: "
                               f"`exo-v2v rehearsal destroy {rh.name}` and rehearse again, or --accept-inconsistent")
    if moved:
        cons["inconsistent"] = moved
        rh.save()
        rh.event(f"WARNING seeding resumed with {', '.join(moved)} past the consistent point (--accept-inconsistent)")
    return {}


def seed(rh, x, st, sh, ex, timeout, port, lk=None):
    """Volumes from snapshots of the production volumes, then the root disk,
    with the production migration's lock held: one point in its replication
    (lk: the lock a group's consistent point still holds; released here)."""
    from .cli import install_agent, wait_receiver, win_support, tunnel_for
    if sh.data.get("seeded"):
        if lk:
            lk.close()
        return
    lk = lk or wait_lock(st, f"rehearsal {rh.name}")
    try:
        t0 = time.time()
        r, sr = st["receiver"], sh["receiver"]
        labels = {LABEL: sh.name, RH_LABEL: rh.name}
        for i, v in enumerate(r["volumes"], 1):
            if any(y["export"] == v["export"] for y in sr["volumes"]):
                continue
            snap = f"{v['name']}-rh-{int(time.time())}"
            ex.snapshot_create(v["name"], snap, labels)
            with Rehearsal.lk:
                rh.data.setdefault("snapshots", []).append(snap)
                rh.save()
            vname = f"v2v-{sh.name}-d{i}"
            ex.volume_create(vname, volume_size_gb(ex.volume(v["name"])), labels, snapshot=snap)
            ex.volume_attach(vname, sr["name"])
            sr["volumes"].append({"name": vname, "export": v["export"], "serial": ex.volume(vname)["id"][:20]})
            sh.save()
            try:
                ex.snapshot_delete(snap)            # the volume is independent of it
                with Rehearsal.lk:
                    rh.data["snapshots"].remove(snap)
                    rh.save()
            except ExoError as e:
                log(f"snapshot {snap} kept until destroy ({str(e)[:80]})")
            log(f"{v['export']}: volume {vname} from a snapshot of {v['name']}")
        install_agent(sh, 1 + len(sr["volumes"]), timeout=timeout, template_mode=True)
        wait_receiver(sh, timeout=timeout)
        log("copying the root disk from the production receiver (through the relay)")
        res = stream_root(rh, st, sh, port)
        log(f"root disk: {human(res['bytes'])} ({human(res['data'])} data) in {res['seconds']}s, {res['mib_s']} MiB/s")
        if sh["plan"]["os_family"] == "windows":
            with tunnel_for(sh) as t:
                win_support(t)
        sh.data["seed"] = {"t": time.time(), "seconds": round(time.time() - t0), "root": res,
                           "volumes": len(sr["volumes"]), "source_cycles": len(st["cycles"])}
        sh.data["seeded"] = time.time()
        sh.set_phase("receiver-ready")
    finally:
        lk.close()


def convert_shadow(rh, x, sh, ex, timeout):
    """The cutover's conversion, on a crash-consistent copy (a running VM's replica)."""
    from .cli import convert, install_agent, stop_and_scale, tunnel_for, wait_receiver
    if sh["phase"] in ("converted", "done"):
        return
    steps = sh.data.setdefault("cutover_steps", {})
    if steps.get("convert_started") and not steps.get("converted"):
        log("a previous conversion did not finish: restarting the copy's agent first")
        install_agent(sh, 1 + len(sh["receiver"]["volumes"]), timeout=timeout, template_mode=True)
        wait_receiver(sh, timeout=timeout)
    sh.set_phase("cutover")
    with tunnel_for(sh) as t:
        convert(sh, ex, t, extra={"crash_consistent": True})
    sh.set_phase("converted")
    stop_and_scale(sh, ex)


def relay_wait_port(rh, ip, port, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if rh.relay(f"nc -z -w 3 {ip} {port}", check=False, timeout=30).returncode == 0:
            return time.time()
        time.sleep(5)
    return None


def relay_check(rh, h, ctx, vm, timeout):
    """A health check run on the relay. Returns (ok, detail)."""
    from .group import expand
    cmds, what = [], []
    if h.get("tcp"):
        cmds.append(f"nc -z -w 5 {shlex.quote(ctx[vm]['public_ip'])} {int(h['tcp'])}")
        what.append(f"tcp/{h['tcp']}")
    if h.get("http"):
        url = expand(h["http"], ctx, vm)
        c = f"curl -s -m 10 -o /tmp/v2v-hc -w '%{{http_code}}' {shlex.quote(url)} | grep -qE '^[23]'"
        if h.get("expect"):
            c += f" && grep -qF -- {shlex.quote(expand(str(h['expect']), ctx, vm))} /tmp/v2v-hc"
        cmds.append(c)
        what.append(url)
    if h.get("command"):
        cmds.append(expand(h["command"], ctx, vm))
        what.append("command")
    if not cmds:
        return True, "no health check"
    t0 = time.time()
    while True:
        p = rh.relay(" && ".join(cmds), check=False, timeout=120)
        if p.returncode == 0:
            return True, " + ".join(what) + " ok (from the relay)"
        if time.time() - t0 > timeout:
            return False, " + ".join(what) + f" failing: {(p.stderr or p.stdout).strip()[-150:] or 'rc=%d' % p.returncode}"
        time.sleep(5)


def ctx_of(rh):
    out = {}
    for m in rh.data["members"]:
        p = os.path.join(home(), shadow_name(m["migration"]), "state.json")
        if not os.path.exists(p):
            continue
        sh = State.load(shadow_name(m["migration"]))
        privs = {y.get("name") or y["network"]: y["ip"] for y in sh["plan"]["networks"] if y.get("ip")}
        out[m["vm"]] = {"public_ip": sh["receiver"].get("ip", ""), "private_ip": next(iter(privs.values()), ""),
                        "name": sh["receiver"].get("name", ""), "vm": m["vm"],
                        **{f"ip.{k}": v for k, v in privs.items()}}
    return out


def airgap_check(rh, ex):
    problems = []
    names = {x["name"] for x in rh.data["networks"]}
    for m in rh.data["members"]:
        sh = State.load(shadow_name(m["migration"]))
        d = ex.instance(sh["receiver"]["name"])
        if d.get("public-ip") != "none":
            problems.append(f"{m['vm']}: the copy has a public IP ({d.get('public-ip')})")
        extra = set(d.get("private_networks") or []) - names
        if extra:
            problems.append(f"{m['vm']}: the copy is attached to {sorted(extra)}")
    for x in rh.data["networks"]:
        if (ex.privnet(x["name"]).get("options") or {}).get("routers"):
            problems.append(f"{x['name']} hands out a router")
    p = rh.relay("cat /proc/sys/net/ipv4/ip_forward /proc/sys/net/ipv6/conf/all/forwarding; "
                 "sudo nft list chain inet v2v_relay forward", check=False)
    if p.stdout.split()[:2] != ["0", "0"] or "policy drop" not in p.stdout:
        problems.append(f"relay forwarding not off: {p.stdout.strip()[:200]}")
    return problems


# ---------------- commands ----------------

def settings_of(base, a):
    s = {**DEFAULTS, **(base or {})}
    s["access"] = list(s.get("access") or []) + list(getattr(a, "access", None) or [])
    keys = list(s.get("ssh_keys") or [])
    for f in getattr(a, "ssh_key", None) or []:
        keys += [k for k in open(f).read().splitlines() if k.strip()]
    s["ssh_keys"] = keys
    if getattr(a, "ttl", None):
        s["ttl"] = a.ttl
    if getattr(a, "front_cidr", None):
        s["front_cidr"] = a.front_cidr
    s["hosts"] = {**(s.get("hosts") or {}), **dict(h.split("=", 1) for h in getattr(a, "host", None) or [])}
    for c in s["access"]:
        ipaddress.ip_network(c, strict=False)
    ipaddress.ip_network(s["front_cidr"], strict=False)
    s["ttl_s"] = duration(s["ttl"])
    return s


def preflight(rh, sts, ex):
    problems = []
    for vm, st in sts.items():
        if st["phase"] not in ("syncing", "receiver-ready"):
            problems.append(f"{vm}: phase {st['phase']} (needs a synced receiver)")
        elif not st["cycles"]:
            problems.append(f"{vm}: never synced")
        tpl, vis = shadow_template(st["plan"])
        try:
            ex.template_boot_mode(tpl, vis)
        except ExoError:
            problems.append(f"{vm}: receiver template {tpl} ({vis}) not found: rehearsal copies need one")
        for export, r in ((st.data.get("preflight") or {}).get("disks") or {}).items():
            for prob in r.get("problems", []):          # what the cutover would refuse too
                if any(k in prob for k in ("dynamic disk", "Storage Spaces", "fold the volumes")):
                    problems.append(f"{vm} {export}: {prob} (leave it out with --vm, or plan it again)")
    try:
        lim = {x["resource"]: x for x in ex.run("limits")}
        i, pn = lim.get("Compute instances", {}), lim.get("Private networks", {})
        if i and i["used"] + len(sts) + 1 > i["max"]:
            problems.append(f"instance quota: {i['used']} used + {len(sts)} copies + the relay > {i['max']}")
        nets = 1 + len({x["network"] for st in sts.values() for x in st["plan"].get("networks", [])
                        if x["network"] != "public"})
        if pn and pn["used"] + nets > pn["max"]:
            problems.append(f"private network quota: {pn['used']} used + {nets} > {pn['max']}")
    except (ExoError, TypeError, KeyError):
        pass
    return problems


def rehearse(name, members, settings, zone, a, consistent=None):
    """members: [{"vm", "migration", "tier", "health"}]. consistent: a callable
    that brings every member's replica to one point in time and returns (record,
    {migration: held lock}) - group rehearsals with --consistent."""
    from .cli import sender_cidr
    rh = Rehearsal.load(name, must=False)
    if rh.active and rh.data.get("state") == "ready":
        die(f"rehearsal {name} is up (since {time.ctime(rh.data['created'])}): "
            f"`exo-v2v rehearsal destroy {name}` first")
    rh.lock("rehearse")
    if not rh.active:
        for f in ("known_hosts",):             # a new relay, new host keys
            if os.path.exists(os.path.join(rh.dir, f)):
                os.remove(os.path.join(rh.dir, f))
        rh.data = {"name": name, "zone": zone, "created": time.time(), "state": "building", "steps": {},
                   "settings": settings, "members": members, "events": []}
        rh.save()
    rh.data["state"] = "building"
    rh.data.pop("error", None)
    steps, ex = rh.data["steps"], Exo(zone, a.account)
    members = rh.data["members"]
    sts = {m["vm"]: State.load(m["migration"]) for m in members}
    tiers = sorted({m["tier"] for m in members})
    try:
        if "preflight" not in steps:
            problems = preflight(rh, sts, ex)
            if problems:
                rh.data["state"] = "destroyed"           # nothing created yet
                rh.save()
                die("preflight:\n  " + "\n  ".join(problems))
            rh.mark("preflight")
        if "networks" not in steps:
            rh.data["networks"] = layout(rh, sts, ex)
            rh.save()
            from .plan import privnet_range
            for x in rh.data["networks"]:
                if not ex.exists("privnet", x["name"]):
                    start, end, mask = privnet_range(x["cidr"])
                    ex.privnet_create(x["name"], start, end, mask, f"exo-v2v rehearsal {name} (isolated)",
                                      dns_servers=[x["relay_ip"]])
            rh.event("rehearsal networks: " + ", ".join(f"{x['name']} {x['cidr']}" for x in rh.data["networks"]))
            rh.mark("networks")
        if "relay" not in steps:
            sender = next((st["receiver"].get("sender_cidr") for st in sts.values()
                           if st["receiver"].get("sender_cidr")), None) or sender_cidr(a)
            create_relay(rh, ex, sender, a.timeout)
            rh.event(f"relay {rh.data['relay']['name']} at {rh.data['relay']['ip']} (SSH only, no forwarding)")
            rh.mark("relay")
        seed_egress(rh, sts, ex)
        if "shadows" not in steps:
            res = parallel(lambda m: make_shadow(rh, m, sts[m["vm"]], ex, a.timeout), members)
            bad = {k: v for k, v in res.items() if v}
            if bad:
                raise RehearsalFailure(f"creating the copies failed: {bad}")
            rh.mark("shadows")
        if "airgap" not in steps:
            problems = airgap_check(rh, ex)
            if problems:
                raise RehearsalFailure("airgap check failed (no copy was started): " + "; ".join(problems))
            rh.event("airgap check passed: no public IP, rehearsal networks only, no router, relay forwarding off")
            rh.mark("airgap")
        if "seeded" not in steps:
            for m in members:                              # the relay's SSH from each production receiver
                ip = sts[m["vm"]]["receiver"]["ip"]
                if ip not in rh.data.setdefault("seed_rules", []):
                    try:
                        ex.sg_rule_add(rh.data["relay"]["sg"], f"{ip}/32", 22, desc=f"exo-v2v seed from {m['vm']}")
                    except ExoError:
                        pass
                    rh.data["seed_rules"].append(ip)
                    rh.save()
            ports = {m["vm"]: 10900 + i for i, m in enumerate(members)}
            locks = {}
            if consistent:
                locks = consistent_point(rh, members, consistent, getattr(a, "accept_inconsistent", False))
                sts = {m["vm"]: State.load(m["migration"]) for m in members}
            try:
                res = parallel(lambda m: seed(rh, m, sts[m["vm"]], State.load(shadow_name(m["migration"])), ex,
                                              a.timeout, ports[m["vm"]], lk=locks.pop(m["migration"], None)),
                               members, workers=4 if not locks else len(members))
            finally:
                for lk in locks.values():
                    lk.close()
            bad = {k: v for k, v in res.items() if v}
            if bad:
                raise RehearsalFailure(f"seeding failed: {bad}")
            rh.event("every copy seeded (production replication goes on)")
            rh.mark("seeded")
        if "converted" not in steps:
            res = parallel(lambda m: convert_shadow(rh, m, State.load(shadow_name(m["migration"])), ex, a.timeout),
                           members)
            bad = {k: v for k, v in res.items() if v}
            if bad:
                raise RehearsalFailure(f"conversion failed: {bad}")
            rh.event("every copy converted")
            rh.mark("converted")
        ctx = ctx_of(rh)
        for tier in tiers:
            if f"tier{tier}_up" in steps:
                continue

            def up(m):
                sh = State.load(shadow_name(m["migration"]))
                r = sh["receiver"]
                port = 3389 if sh["plan"]["os_family"] == "windows" else 22
                t0 = time.time()
                if ex.instance(r["name"]).get("state") != "running":
                    ex.instance_start(r["name"])
                upat = relay_wait_port(rh, r["ip"], port, duration(getattr(a, "boot_timeout", "15m")))
                h = m.get("health") or {}
                ok, detail = relay_check(rh, h, ctx, m["vm"], duration(h.get("timeout") or "10m")) \
                    if upat else (False, f"port {port} never answered")
                sh.data.setdefault("cutover_steps", {})["started"] = t0
                sh.data["rehearsal_boot_s"] = round((upat or time.time()) - t0)
                sh.data["rehearsal_health"] = {"ok": ok, "detail": detail}
                sh.set_phase("done")
                log(f"{'healthy' if ok else 'UNHEALTHY'}: {detail}")
                if not ok:
                    raise RehearsalFailure(detail)
            res = parallel(up, [m for m in members if m["tier"] == tier])
            rh.mark(f"tier{tier}_up")
            bad = {k: v for k, v in res.items() if v}
            rh.event(f"tier {tier} up" + (f"; unhealthy: {bad}" if bad else " and healthy"))
        rh.data["state"] = "ready"
        rh.data["ready"] = time.time()
        rh.data["report"] = build_report(rh)
        rh.save()
        print_report(rh)
        print(f"\nconnect: `exo-v2v rehearsal connect {name}`   destroy: `exo-v2v rehearsal destroy {name}` "
              f"(automatic after {rh.data['settings']['ttl']} with `exo-v2v gc`)")
    except RehearsalFailure as e:
        rh.data["state"] = "failed"
        rh.data["error"] = str(e)
        rh.save()
        rh.event(f"REHEARSAL STOPPED: {e}")
        die(f"rehearsal stopped: {e}\n  fix it and run the same command again (it resumes), "
            f"or `exo-v2v rehearsal destroy {name}`")


# ---------------- report, connect, destroy ----------------

def dns_queries(rh):
    p = rh.relay("sudo cat /var/log/v2v-relay-dns.log 2>/dev/null", check=False)
    who = {}
    for m in rh.data["members"]:
        p2 = os.path.join(home(), shadow_name(m["migration"]), "state.json")
        if os.path.exists(p2):
            sh = State.load(shadow_name(m["migration"]))
            who[sh["receiver"].get("ip")] = m["vm"]
            for y in sh["plan"]["networks"]:
                if y.get("ip"):
                    who[y["ip"]] = m["vm"]
    seen = {}
    for line in p.stdout.splitlines():
        mo = re.search(r"query\[(\w+)\] (\S+) from (\S+)", line)
        if mo:
            seen.setdefault(mo.group(2), set()).add(who.get(mo.group(3), mo.group(3)))
    return {k: sorted(v) for k, v in sorted(seen.items())}


def build_report(rh):
    rows, conv = [], []
    for m in rh.data["members"]:
        p = os.path.join(home(), shadow_name(m["migration"]), "state.json")
        if not os.path.exists(p):
            continue
        sh, st = State.load(shadow_name(m["migration"])), State.load(m["migration"])
        cs = sh.data.get("cutover_steps", {})
        c = round(cs["flushed"] - cs["convert_started"]) if cs.get("flushed") and cs.get("convert_started") else None
        deltas = [c for c in st["cycles"] if not c.get("final") and c.get("disks")
                  and all(d.get("mode") == "delta" for d in c["disks"].values())]
        last = deltas[-1]["seconds"] if deltas else None          # the final cycle is a delta
        rows.append({"vm": m["vm"], "tier": m["tier"], "seed_s": (sh.data.get("seed") or {}).get("seconds"),
                     "root": (sh.data.get("seed") or {}).get("root"), "convert_s": c,
                     "boot_s": sh.data.get("rehearsal_boot_s"), "health": sh.data.get("rehearsal_health"),
                     "last_cycle_s": last, "address": sh["receiver"].get("ip"),
                     "private": [y["ip"] for y in sh["plan"]["networks"] if y.get("ip")]})
        if c is not None:
            conv.append((last or 0) + c)
    tiers = sorted({r["tier"] for r in rows})
    boots = sum(max([r["boot_s"] or 0 for r in rows if r["tier"] == t] or [0]) for t in tiers)
    predicted = (max(conv) + boots + 15 * len(tiers)) if conv else None
    return {"t": time.time(), "members": rows, "predicted_window_s": predicted,
            "consistency": rh.data.get("consistency"),
            "dns": dns_queries(rh) if rh.data.get("relay", {}).get("ip") else {}}


def print_report(rh):
    rep = rh.data.get("report") or build_report(rh)
    print(f"\nrehearsal {rh.name}: {rh.data.get('state')}  relay {rh.data.get('relay', {}).get('ip', '-')}")
    print(f"  {'tier':<4} {'vm':<12} {'seed':>7} {'convert':>8} {'boot':>6}  {'address':<16} health")
    for r in sorted(rep["members"], key=lambda r: (r["tier"], r["vm"])):
        h = r.get("health") or {}
        f = lambda v: f"{v}s" if v is not None else "-"
        print(f"  {r['tier']:<4} {r['vm']:<12} {f(r['seed_s']):>7} {f(r['convert_s']):>8} {f(r['boot_s']):>6}  "
              f"{(r['address'] or '-'):<16} {'ok' if h.get('ok') else 'FAILED' if h else '-'} {h.get('detail', '')}")
    c = rep.get("consistency")
    if c:
        print(f"  consistent point: {c['snapshot']}, snapshots within {c['spread_s']}s, {c['mode']}"
              + "".join(f"; {vm}: not quiesced ({v['reason']})" for vm, v in c["members"].items()
                        if c.get("quiesce") and not v["quiesced"]))
        slow = sorted(((v.get("end_s", 0), vm) for vm, v in c["members"].items()), reverse=True)[:3]
        if slow and slow[0][0]:
            print("    slowest snapshots done at " + ", ".join(f"{vm} +{t}s" for t, vm in slow))
        if c.get("inconsistent"):
            print(f"    seeded past the point: {', '.join(c['inconsistent'])}")
    else:
        print("  members seeded from their own last cycles (not one point in time: --consistent)")
    if rep.get("predicted_window_s"):
        print(f"  predicted cutover window: ~{rep['predicted_window_s'] // 60} min "
              f"(last delta + measured conversion of the slowest member + boots per tier)")
    if rep.get("dns"):
        print("  names the copies tried to resolve (refused unless listed in `hosts`):")
        for k, v in rep["dns"].items():
            print(f"    {k:<40} {', '.join(v)}")


def cmd_status(a):
    names = [a.name] if a.name else Rehearsal.all()
    for n in names:
        rh = Rehearsal.load(n)
        age = time.time() - rh.data.get("created", time.time())
        ttl = rh.data.get("settings", {}).get("ttl_s")
        left = f", destroyed by `gc` in {int((ttl - age) // 60)} min" if ttl and rh.active and ttl > age else \
            ", past its TTL" if ttl and rh.active else ""
        print(f"{n}: {rh.data.get('state')} at {rh.data.get('step')}, {int(age // 60)} min old{left}")
        if rh.data.get("error"):
            print(f"  error: {rh.data['error']}")
        if rh.active and rh.data.get("relay", {}).get("ip"):
            print(f"  relay {rh.data['relay']['ip']}; copies: " + ", ".join(
                f"{m['vm']} {ctx.get('public_ip')}" for m in rh.data["members"]
                for ctx in [ctx_of(rh).get(m["vm"], {})]))


def cmd_report(a):
    rh = Rehearsal.load(a.name)
    if rh.active and rh.data.get("relay", {}).get("ip"):
        rh.data["report"] = build_report(rh)
        rh.save()
    print_report(rh)


def cmd_connect(a):
    rh = Rehearsal.load(a.name)
    if not rh.active or not rh.data.get("relay", {}).get("ip"):
        die(f"rehearsal {a.name} is not up")
    n, relay = rh.name, rh.data["relay"]["ip"]
    ctx = ctx_of(rh)
    print(f"# ~/.ssh/config for rehearsal {n}: the copies are reachable only through the relay")
    print(f"Host {n}-relay\n    HostName {relay}\n    User rehearsal\n    # IdentityFile ~/.ssh/<your key listed in rehearsal.ssh_keys>\n")
    for vm, c in ctx.items():
        print(f"Host {n}-{vm}\n    HostName {c['private_ip'] or c['public_ip']}\n    ProxyJump {n}-relay\n"
              f"    # front address {c['public_ip']}" + (f", private {c['private_ip']}" if c["private_ip"] else "") + "\n")
    print("# port forwards, e.g. a web front end and RDP:")
    for vm, c in ctx.items():
        print(f"#   ssh -N -L 8080:{c['public_ip']}:80 rehearsal@{relay}      # {vm}")
        break
    if a.forward:
        args = []
        for spec in a.forward:
            parts = spec.split(":")
            vm, port = parts[0], int(parts[1])
            local = int(parts[2]) if len(parts) > 2 else port + 10000
            if vm not in ctx:
                die(f"no copy {vm} (have {', '.join(ctx)})")
            args += ["-L", f"127.0.0.1:{local}:{ctx[vm]['public_ip']}:{port}"]
            print(f"forwarding 127.0.0.1:{local} -> {vm} {ctx[vm]['public_ip']}:{port}")
        t = rh.relay_tunnel()
        print("Ctrl-C to stop")
        try:
            subprocess.run(t.ssh_base() + ["-N", *args, f"debian@{relay}"])
        except KeyboardInterrupt:
            pass


def destroy(rh, a):
    from .cli import cleanup
    ex = Exo(rh.data["zone"], a.account)
    n = rh.name
    rh.data["state"] = "destroying"
    rh.save()
    for m in rh.data.get("members", []):
        sname = shadow_name(m["migration"])
        if os.path.exists(os.path.join(home(), sname, "state.json")):
            sh = State.load(sname)
            if sh["receiver"].get("name"):
                with log_prefix(m["vm"]):
                    ns = argparse.Namespace(account=a.account, delete_instance=True, force=True,
                                            delete_rollback_volumes=True, rename=None)
                    cleanup(sh, ns)
            shutil.rmtree(sh.dir, ignore_errors=True)
    # safety net, by name: everything a rehearsal creates is v2v-<name>-...-rh...
    pre = f"v2v-{n}"
    for i in ex.instances():
        if i["name"].startswith(pre) and (i["name"].endswith("-rh") or i["name"].endswith("-rh-relay")):
            ex.instance_delete(i["name"])
            log(f"deleted instance {i['name']}")
    for v in ex.volumes():
        if v["name"].startswith(pre) and "-rh-" in v["name"]:
            try:
                ex.volume_delete(v["name"])
                log(f"deleted volume {v['name']}")
            except ExoError as e:
                log(f"volume {v['name']}: {e}")
    for s in ex.snapshots():
        if s.get("name", "").startswith(pre) and "-rh-" in s.get("name", ""):
            try:
                ex.snapshot_delete(s["name"])
                log(f"deleted snapshot {s['name']}")
            except ExoError as e:
                log(f"snapshot {s['name']}: {e}")
    for x in rh.data.get("networks", []):
        for i in range(12):                     # detaching takes a moment after instance deletion
            try:
                if ex.exists("privnet", x["name"]):
                    ex.privnet_delete(x["name"])
                    log(f"deleted network {x['name']}")
                break
            except ExoError:
                time.sleep(10)
    for mig in rh.data.get("seed_egress", []):
        st = State.load(mig) if os.path.exists(os.path.join(home(), mig, "state.json")) else None
        sg = st and st["receiver"].get("sg")
        try:
            for x in (ex.sg_egress_rules(sg) if sg else []):
                if x.get("description") == seed_egress_desc(rh):
                    ex.sg_rule_delete(sg, x["id"])
                    log(f"{mig}: egress to the relay closed again")
        except ExoError as e:
            log(f"{mig}: egress rule to the relay left in {sg} (harmless; gone at cleanup): {e}")
    sg = rh.data.get("relay", {}).get("sg")
    if sg and ex.exists("sg", sg):
        for i in range(12):
            try:
                ex.sg_delete(sg)
                log(f"deleted security group {sg}")
                break
            except ExoError:
                time.sleep(10)
    rh.data.update(state="destroyed", destroyed=time.time())
    rh.save()
    rh.event(f"rehearsal {n} destroyed")


def cmd_destroy(a):
    rh = Rehearsal.load(a.name)
    rh.lock("destroy")
    destroy(rh, a)


def cmd_gc(a):
    for n in Rehearsal.all():
        rh = Rehearsal.load(n)
        ttl = (rh.data.get("settings") or {}).get("ttl_s")
        if rh.active and ttl and time.time() - rh.data.get("created", 0) > ttl:
            log(f"rehearsal {n} is past its TTL ({rh.data['settings']['ttl']}): destroying it")
            rh.lock("gc")
            destroy(rh, a)
            from . import alerts
            alerts.send("rehearsal-expired", f"rehearsal {n} destroyed at the end of its TTL "
                        f"({rh.data['settings']['ttl']})", rehearsal=n)


def cmd_rehearse(a):
    """A single migration (a group of one)."""
    st = State.load(a.name)
    if st.data.get("group"):
        die(f"{a.name} belongs to group {st['group']}: `exo-v2v group rehearse {st['group']}`")
    h = {}
    if a.tcp:
        h["tcp"] = a.tcp
    if a.http:
        h["http"] = a.http
    rehearse(a.name, [{"vm": st["plan"]["vm"], "migration": a.name, "tier": 1, "health": h}],
             settings_of({}, a), st["plan"]["zone"], a)


def add_options(s):
    s.add_argument("--access", action="append", metavar="CIDR", help="may SSH to the relay (repeatable)")
    s.add_argument("--ssh-key", action="append", metavar="FILE", help="public key(s) for the relay's `rehearsal` user")
    s.add_argument("--ttl", help="destroyed by `exo-v2v gc` after this long (default 8h)")
    s.add_argument("--front-cidr", help="network standing in for the public interface (default 100.127.0.0/24)")
    s.add_argument("--host", action="append", metavar="NAME=IP", help="a name the relay's DNS answers")
    s.add_argument("--timeout", type=int, default=1800)
    s.add_argument("--boot-timeout", default="15m")


def add_parser(sub):
    s = sub.add_parser("rehearse", help="start an airgapped copy of a migration (rehearsal)")
    s.add_argument("name")
    add_options(s)
    s.add_argument("--tcp", type=int, help="health check: a TCP port of the copy")
    s.add_argument("--http", help="health check: a URL ({public_ip} = the copy's front address)")
    s.set_defaults(fn=cmd_rehearse)
    rp = sub.add_parser("rehearsal", help="status, access, report and destruction of rehearsals")
    rs = rp.add_subparsers(dest="rcmd", required=True)
    s = rs.add_parser("status"); s.add_argument("name", nargs="?"); s.set_defaults(fn=cmd_status)
    s = rs.add_parser("report"); s.add_argument("name"); s.set_defaults(fn=cmd_report)
    s = rs.add_parser("connect", help="ssh config for the copies; --forward VM:PORT[:LOCAL] from here")
    s.add_argument("name"); s.add_argument("--forward", action="append", metavar="VM:PORT[:LOCAL]")
    s.set_defaults(fn=cmd_connect)
    s = rs.add_parser("destroy"); s.add_argument("name"); s.set_defaults(fn=cmd_destroy)
    s = sub.add_parser("gc", help="destroy rehearsals past their TTL (run it from cron)")
    s.set_defaults(fn=cmd_gc)
