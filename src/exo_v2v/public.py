"""The public side of a cutover: Elastic IPs, reverse DNS, DNS records.

- An Elastic IP is reserved at `create` (its address is known before the window:
  partners, firewalls and DNS can be told ahead), attached to the stopped instance
  at cutover, and configured in the guest by the conversion (Linux: on lo through
  v2v-eip.service; Windows: the public NIC made static plus the Elastic IP).
- Reverse DNS (PTR) is set at `create`, on the Elastic IP or the instance.
- DNS records on Exoscale DNS: `create` saves their values and lowers their TTL,
  the cutover points them at the new address once the instance (or the group's
  last tier) is healthy, `rollback` puts the old values back, `cleanup` restores
  the original TTL. One A record per name; other DNS providers: hooks. After the
  switch, the zone's own nameservers are asked until they all serve the new
  address (Exoscale's took 1 to 62 s in the lab).
"""
import os
import socket
import struct
import time

from .exo import ExoError
from .util import log


def address(st):
    """The address the world reaches the migrated instance at."""
    r = st["receiver"]
    return (r.get("eip") or {}).get("ip") or r.get("ip")


def reserve_eip(st, ex):
    """Plan's public.elastic_ip ("new" or an existing address) -> receiver.eip."""
    want = (st["plan"].get("public") or {}).get("elastic_ip")
    r = st["receiver"]
    if not want or r.get("eip"):
        return r.get("eip")
    if want == "new":
        e = ex.eip_by_description(f"v2v-{st.name}")      # reserved by an interrupted create
        if not e:
            e = ex.eip_create(f"v2v-{st.name}")
        r["eip"] = {"ip": e["ip_address"], "id": e.get("id"), "created": True}
        log(f"Elastic IP {e['ip_address']} reserved for {st.name}")
    else:
        try:
            e = ex.eip(want)
        except ExoError as err:
            raise ExoError(f"Elastic IP {want}: {err}") from None
        others = [i for i in e.get("instances") or [] if i != r.get("name")]
        if others:
            raise ExoError(f"Elastic IP {want} is attached to {', '.join(others)}")
        r["eip"] = {"ip": e["ip_address"], "id": e.get("id"), "created": False}
        log(f"Elastic IP {want} kept for {st.name}")
    st.save()
    return r["eip"]


def set_reverse_dns(st, ex):
    name = (st["plan"].get("public") or {}).get("reverse_dns")
    r = st["receiver"]
    if not name or r.get("reverse_dns") == name:
        return
    if r.get("eip"):
        ex.eip_reverse_dns(r["eip"]["ip"], name)
    else:
        ex.instance_reverse_dns(r["name"], name)
    r["reverse_dns"] = name
    st.save()
    log(f"reverse DNS {address(st)} -> {name}")


def attach_eip(st, ex):
    """At cutover, the instance stopped (idempotent)."""
    r = st["receiver"]
    e = r.get("eip")
    if not e or e.get("attached"):
        return
    if r["name"] not in (ex.eip(e["ip"]).get("instances") or []):
        ex.eip_attach(r["name"], e["ip"])
    e["attached"] = time.time()
    st.save()
    log(f"Elastic IP {e['ip']} attached to {r['name']}")


def release_eip(st, ex, delete=False):
    """cleanup: an Elastic IP exo-v2v reserved is deleted only on request."""
    r = st["receiver"]
    e = r.get("eip")
    if not e:
        return
    if not (delete and e.get("created")):
        log(f"Elastic IP {e['ip']} kept" + ("" if e.get("created") else " (it was yours before)")
            + (": --delete-elastic-ips releases it" if e.get("created") and not delete else ""))
        return
    try:
        if r.get("name") and r["name"] in (ex.eip(e["ip"]).get("instances") or []):
            ex.eip_detach(r["name"], e["ip"])
        ex.eip_delete(e["ip"])
        log(f"Elastic IP {e['ip']} deleted")
        r.pop("eip")
        st.save()
    except ExoError as err:
        log(f"Elastic IP {e['ip']} kept: {err}")


# ---------------- DNS ----------------

def split(fqdn, domains):
    """('example.com', 'www') for www.example.com: the longest Exoscale DNS domain
    that ends the name; '' is the apex."""
    fqdn = fqdn.rstrip(".").lower()
    best = max((d for d in domains if fqdn == d.lower() or fqdn.endswith("." + d.lower())),
               key=len, default=None)
    if not best:
        raise ExoError(f"{fqdn}: no Exoscale DNS domain of this account holds it "
                       f"(have: {', '.join(domains) or 'none'})")
    return best, fqdn[:-len(best)].rstrip(".")


def check(ex, names):
    """Problems with DNS names to switch (for plan): each must be one A record."""
    try:
        domains = ex.dns_domains()
    except ExoError as e:
        return [f"DNS: cannot list the Exoscale DNS domains: {e}"]
    errs = []
    for n in names:
        try:
            dom, rel = split(n, domains)
            recs = ex.dns_records(dom, rel)
        except ExoError as e:
            errs.append(f"DNS {n}: {e}")
            continue
        if len(recs) != 1:
            errs.append(f"DNS {n}: {len(recs)} A records (one is needed: create it, or remove extras)")
    return errs


def prepare(ex, wanted, ttl):
    """wanted: [(fqdn, migration)]. Saves each record's value and TTL and lowers
    the TTL to `ttl`; returns the records (stored in the state)."""
    domains = ex.dns_domains()
    out = []
    for fqdn, mig in wanted:
        dom, rel = split(fqdn, domains)
        recs = ex.dns_records(dom, rel)
        if len(recs) != 1:
            raise ExoError(f"DNS {fqdn}: {len(recs)} A records, one expected")
        rec = recs[0]
        d = {"fqdn": fqdn, "domain": dom, "name": rel, "id": rec["id"], "migration": mig,
             "old": rec["content"], "old_ttl": rec["ttl"], "ttl": min(ttl, rec["ttl"] or ttl),
             "lowered": None}
        if rec["ttl"] > ttl:
            ex.dns_update(dom, rec["id"], ttl=ttl)
            d["lowered"] = time.time()
            log(f"DNS {fqdn}: TTL {rec['ttl']}s -> {ttl}s (resolvers may keep the old value "
                f"{rec['ttl']}s: cut over after that)")
        out.append(d)
    return out


PROPAGATION_S = 120     # Exoscale DNS: a change reaches every nameserver within about a minute
SERVED_WAIT_S = 180
STREAK = 10             # answers in a row before a nameserver address counts as serving a change


def not_ready(recs, now=None):
    """Records whose lowered TTL is not in effect everywhere yet."""
    now = now or time.time()
    return [r["fqdn"] for r in recs
            if r.get("lowered") and now - r["lowered"] < r["old_ttl"] + PROPAGATION_S]


def switch(ex, recs, addr_of):
    """Points each record at addr_of(migration). Idempotent."""
    for r in recs:
        new = addr_of(r["migration"])
        if r.get("new") == new and r.get("switched") and not r.get("restored"):
            continue
        ex.dns_update(r["domain"], r["id"], content=new)
        r.update(new=new, switched=time.time(), restored=None)
        log(f"DNS {r['fqdn']}: {r['old']} -> {new} (TTL {r['ttl']}s)")


def describe(r):
    if r.get("restored"):
        return f"DNS {r['fqdn']}: {r['old']} (restored)"
    if r.get("switched"):
        return f"DNS {r['fqdn']}: -> {r['new']}" + (f" (served after {r['served_s']}s)" if r.get("served_s") else "")
    return f"DNS {r['fqdn']}: {r['old']}, TTL {r['ttl']}s"


def query_a(server, fqdn, timeout=2.0, port=53):
    """The A addresses `server` answers for `fqdn` (no recursion asked), or None
    when it does not answer."""
    qid = struct.unpack(">H", os.urandom(2))[0]
    q = struct.pack(">HHHHHH", qid, 0, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p for p in fqdn.rstrip(".").encode("idna").split(b".")) + b"\0"
    q += struct.pack(">HH", 1, 1)
    try:
        addr = socket.getaddrinfo(server, port, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(q, addr)
            while True:
                b, _ = s.recvfrom(4096)
                if b[:2] == q[:2]:
                    break
    except OSError:
        return None
    return parse_a(b)


def parse_a(b):
    def skip(i):
        while b[i]:
            if b[i] & 0xC0 == 0xC0:
                return i + 2
            i += 1 + b[i]
        return i + 1
    try:
        _, flags, qd, an = struct.unpack(">HHHH", b[:8])
        if flags & 0xF not in (0, 3):           # an error other than NXDOMAIN: no answer
            return None
        i = 12
        for _ in range(qd):
            i = skip(i) + 4
        out = []
        for _ in range(an):
            i = skip(i)
            rtype, rclass, _ttl, n = struct.unpack(">HHIH", b[i:i + 10])
            i += 10
            if rtype == 1 and rclass == 1 and n == 4:
                out.append(socket.inet_ntoa(b[i:i + 4]))
            i += n
        return out
    except (IndexError, struct.error):
        return None


def nameservers(ex, domain):
    try:
        return sorted(r["content"].rstrip(".") for r in ex.dns_records(domain, "", "NS"))
    except ExoError as e:
        log(f"DNS {domain}: nameservers not listed ({e})")
        return []


def ipv4(name):
    try:
        return sorted({x[4][0] for x in socket.getaddrinfo(name, 53, socket.AF_INET, socket.SOCK_DGRAM)})
    except OSError:
        return []


def wait_served(ex, recs, timeout=SERVED_WAIT_S, query=query_a, resolve=ipv4, sleep=time.sleep, clock=time.time):
    """After switch(): asks every address of each zone's nameservers until each
    has answered the new address STREAK times in a row (one address fronts
    several servers that update one by one); records served_s, seconds after
    the switch. Never fails: late servers, or none answering (UDP 53 closed on
    the way), are logged."""
    todo = [r for r in recs if r.get("switched") and not r.get("restored") and r.get("served_s") is None]
    addrs = {d: sorted({ip for n in nameservers(ex, d) for ip in resolve(n)}) for d in {r["domain"] for r in todo}}
    streak = {(r["fqdn"], ip): 0 for r in todo for ip in addrs[r["domain"]]}
    answered, t0 = set(), clock()
    while True:
        for r in todo:
            for ip in addrs[r["domain"]]:
                k = (r["fqdn"], ip)
                for _ in range(min(5, STREAK - streak[k])):
                    got = query(ip, r["fqdn"])
                    if got is not None:
                        answered.add(ip)
                    streak[k] = streak[k] + 1 if got == [r["new"]] else 0
                    if not streak[k]:
                        break
            if r.get("served_s") is None and addrs[r["domain"]] \
                    and all(streak[(r["fqdn"], ip)] >= STREAK for ip in addrs[r["domain"]]):
                r["served_s"] = round(clock() - r["switched"], 1)
                log(f"DNS {r['fqdn']}: {r['new']} served by every address of its nameservers "
                    f"{r['served_s']}s after the switch")
        late = [k for k, v in streak.items() if v < STREAK]
        if not late or clock() - t0 > timeout or (not answered and clock() - t0 > 10):
            break
        sleep(3)
    if late and not answered:
        log("DNS: no nameserver answered from here (UDP 53 closed?): not checked")
        return
    for r in todo:
        ips = sorted(ip for f, ip in late if f == r["fqdn"])
        if ips:
            log(f"WARNING DNS {r['fqdn']}: {', '.join(ips)} still answer other addresses than "
                f"{r['new']} {round(clock() - r['switched'])}s after the switch (they should follow)")


def restore(ex, recs):
    """rollback: the old values back (the TTL stays low until cleanup)."""
    for r in recs:
        if r.get("switched") and not r.get("restored"):
            ex.dns_update(r["domain"], r["id"], content=r["old"])
            r["restored"] = time.time()
            log(f"DNS {r['fqdn']}: back to {r['old']}")


def finalize(ex, recs):
    """cleanup: the original TTL back."""
    for r in recs:
        if r.get("lowered") and not r.get("ttl_restored"):
            try:
                ex.dns_update(r["domain"], r["id"], ttl=r["old_ttl"])
                r["ttl_restored"] = time.time()
                log(f"DNS {r['fqdn']}: TTL back to {r['old_ttl']}s")
            except ExoError as e:
                log(f"DNS {r['fqdn']}: TTL not restored: {e}")
