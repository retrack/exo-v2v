"""Public side of a cutover (public.py, group manifest, plan): Elastic IPs, reverse
DNS, DNS records, renumbering, security group rules. A fake `exo` stands in for
the Exoscale API.

Run: python3 -m pytest -q tests/
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "exo_v2v", "receiver"))

import guestfix  # noqa: E402
from exo_v2v import cli, group, plan, public  # noqa: E402
from exo_v2v.exo import ExoError  # noqa: E402
from exo_v2v.state import State  # noqa: E402


class FakeExo:
    def __init__(self):
        self.domains = ["example.com", "shop.example.com"]
        self.records = {("example.com", "www"): [{"id": "11", "name": "www", "type": "A",
                                                  "content": "198.51.100.7", "ttl": 3600}],
                        ("shop.example.com", ""): [{"id": "21", "name": "", "type": "A",
                                                    "content": "198.51.100.8", "ttl": 300}],
                        ("example.com", "two"): [{"id": "31"}, {"id": "32"}]}
        self.eip_table = {"203.0.113.5": {"ip_address": "203.0.113.5", "id": "e5", "instances": []}}
        self.calls = []
        self.rules = {}

    def dns_domains(self):
        return self.domains

    def dns_records(self, domain, name=None, rtype="A"):
        return [dict(r) for r in self.records.get((domain, name or ""), [])]

    def dns_update(self, domain, rid, content=None, ttl=None):
        self.calls.append(("dns", domain, rid, content, ttl))
        for recs in self.records.values():
            for r in recs:
                if r.get("id") == rid:
                    r.update({k: v for k, v in (("content", content), ("ttl", ttl)) if v})

    def eip_by_description(self, desc):
        return next((e for e in self.eip_table.values() if e.get("description") == desc), None)

    def eip_create(self, desc):
        e = {"ip_address": "203.0.113.9", "id": "e9", "instances": [], "description": desc}
        self.eip_table[e["ip_address"]] = e
        self.calls.append(("eip_create", desc))
        return e

    def eip(self, ip):
        if ip not in self.eip_table:
            raise ExoError(f"{ip}: not found")
        return self.eip_table[ip]

    def eip_attach(self, inst, ip):
        self.eip_table[ip]["instances"].append(inst)
        self.calls.append(("attach", inst, ip))

    def eip_detach(self, inst, ip):
        self.eip_table[ip]["instances"].remove(inst)
        self.calls.append(("detach", inst, ip))

    def eip_delete(self, ip):
        self.eip_table.pop(ip)
        self.calls.append(("eip_delete", ip))

    def eip_reverse_dns(self, ip, name):
        self.calls.append(("ptr-eip", ip, name))

    def instance_reverse_dns(self, inst, name):
        self.calls.append(("ptr-instance", inst, name))

    def sg(self, name):
        return {"ingress_rules": self.rules.get(name, [])}

    def sg_rule_delete(self, sg, rid):
        self.calls.append(("rule-delete", sg, rid))

    def run(self, *args, json_out=True):
        self.calls.append(("run",) + args)


def test_split_and_check():
    ex = FakeExo()
    assert public.split("www.example.com", ex.domains) == ("example.com", "www")
    assert public.split("shop.example.com.", ex.domains) == ("shop.example.com", "")     # longest wins
    with pytest.raises(ExoError, match="no Exoscale DNS domain"):
        public.split("www.example.org", ex.domains)
    errs = public.check(ex, ["www.example.com", "two.example.com", "none.example.com", "x.example.net"])
    assert len(errs) == 3 and "2 A records" in errs[0] and "0 A records" in errs[1]


def test_dns_prepare_switch_restore_finalize():
    ex = FakeExo()
    recs = public.prepare(ex, [("www.example.com", "shop-lb01"), ("shop.example.com", "shop-lb01")], 60)
    assert [(c[2], c[4]) for c in ex.calls] == [("11", 60), ("21", 60)]        # TTLs lowered
    assert recs[0]["old"] == "198.51.100.7" and recs[0]["old_ttl"] == 3600
    assert public.not_ready(recs, now=recs[0]["lowered"] + 100) == ["www.example.com", "shop.example.com"]
    assert public.not_ready(recs, now=recs[0]["lowered"] + 4000) == []
    public.switch(ex, recs, lambda mig: "203.0.113.9")
    public.switch(ex, recs, lambda mig: "203.0.113.9")                        # idempotent
    assert [c[3] for c in ex.calls[2:]] == ["203.0.113.9", "203.0.113.9"]
    public.restore(ex, recs)
    assert ex.records[("example.com", "www")][0]["content"] == "198.51.100.7"
    public.switch(ex, recs, lambda mig: "203.0.113.9")                        # a new cutover after a rollback
    assert ex.records[("example.com", "www")][0]["content"] == "203.0.113.9"
    public.finalize(ex, recs)
    assert ex.records[("example.com", "www")][0]["ttl"] == 3600 and ex.records[("shop.example.com", "")][0]["ttl"] == 300


def answer(q, addrs, rcode=0):
    """A nameserver's answer to query `q`, the name compressed to the question."""
    import socket
    import struct
    an = b"".join(b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton(x) for x in addrs)
    return q[:2] + struct.pack(">HHHHH", 0x8400 | rcode, 1, len(addrs), 0, 0) + q[12:] + an


def test_query_a_against_a_local_nameserver():
    import socket
    import threading
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    replies = [["203.0.113.9"], [], ["192.0.2.1", "192.0.2.2"]]
    def serve():
        for addrs in replies:
            q, peer = srv.recvfrom(512)
            assert q[2:4] == b"\0\0" and q[12:].startswith(b"\x03www\x07example\x03com\x00")   # no recursion
            srv.sendto(answer(b"\xff\xff" + q[2:], []), peer)      # someone else's id: ignored
            srv.sendto(answer(q, addrs, rcode=3 if not addrs else 0), peer)
    th = threading.Thread(target=serve, daemon=True)
    th.start()
    port = srv.getsockname()[1]
    assert public.query_a("127.0.0.1", "www.example.com.", port=port) == ["203.0.113.9"]
    assert public.query_a("127.0.0.1", "www.example.com", port=port) == []           # NXDOMAIN
    assert public.query_a("127.0.0.1", "www.example.com", port=port) == ["192.0.2.1", "192.0.2.2"]
    th.join(2)
    srv.close()
    assert public.query_a("127.0.0.1", "www.example.com", timeout=0.2, port=port) is None   # nobody there
    assert public.parse_a(b"\0\0\x84\x02\0\1\0\0\0\0\0\0") is None                         # SERVFAIL
    assert public.parse_a(b"\0\0\x84") is None


class NsExo(FakeExo):
    def dns_records(self, domain, name=None, rtype="A"):
        if rtype == "NS":
            return [{"content": n} for n in ("ns1.example.net.", "ns2.example.net.")]
        return super().dns_records(domain, name, rtype)


def test_wait_served():
    ex = NsExo()
    recs = public.prepare(ex, [("www.example.com", "shop-lb01")], 60)
    public.switch(ex, recs, lambda mig: "203.0.113.9")
    now = [recs[0]["switched"]]
    seen = {"ns1.example.net": 0, "ns2.example.net": 0}
    def query(server, fqdn):
        seen[server] += 1
        return ["203.0.113.9"] if server == "ns1.example.net" or seen[server] > 3 else ["198.51.100.7"]
    def sleep(s):
        now[0] += s
    run = dict(resolve=lambda n: [n], sleep=sleep, clock=lambda: now[0])
    public.wait_served(ex, recs, query=query, **run)
    # ns1 serves at once (10 in a row after 2 rounds), ns2 after 3 old answers (one per round): 5 rounds
    assert recs[0]["served_s"] == 12 and seen == {"ns1.example.net": 10, "ns2.example.net": 13}
    public.wait_served(ex, recs, query=query, **run)                          # done: no queries
    assert seen["ns2.example.net"] == 13

    recs[0]["served_s"] = None             # servers behind one address disagree: never 10 in a row
    flip = iter(range(10 ** 6))
    public.wait_served(ex, recs, timeout=60, query=lambda s, f: ["203.0.113.9"] if next(flip) % 3 else ["1.2.3.4"], **run)
    assert recs[0]["served_s"] is None
    t0 = now[0]                            # none answers (UDP 53 closed): stops after 10 s
    public.wait_served(ex, recs, query=lambda s, f: None, **run)
    assert recs[0]["served_s"] is None and now[0] - t0 <= 15
    public.restore(ex, recs)               # restored: nothing to wait for
    public.wait_served(ex, recs, query=lambda s, f: 1 / 0, **run)
    assert public.describe(recs[0]) == "DNS www.example.com: 198.51.100.7 (restored)"


def mig(tmp_path, monkeypatch, pub, nics=()):
    monkeypatch.setenv("EXO_V2V_HOME", str(tmp_path))
    st = State.new("web01", {"public": pub, "networks": []})
    st.data["receiver"] = {"name": "v2v-web01", "ip": "194.182.1.2", "nics": list(nics)}
    st.save()
    return st


def test_elastic_ip_lifecycle(tmp_path, monkeypatch):
    ex = FakeExo()
    st = mig(tmp_path, monkeypatch, cli.public_of("new", "mail.example.com"),
             [{"network": "public", "mac": "06:aa:bb:cc:dd:ee"}])
    public.reserve_eip(st, ex)
    public.reserve_eip(st, ex)                                     # once
    assert st["receiver"]["eip"] == {"ip": "203.0.113.9", "id": "e9", "created": True}
    assert [c for c in ex.calls if c[0] == "eip_create"] == [("eip_create", "v2v-web01")]
    public.set_reverse_dns(st, ex)
    assert ("ptr-eip", "203.0.113.9", "mail.example.com") in ex.calls
    assert public.address(st) == "203.0.113.9"
    nm = cli.netmap_of(st)
    assert nm == [{"mac": "06:aa:bb:cc:dd:ee", "mode": "dhcp"},
                  {"mac": "06:aa:bb:cc:dd:ee", "mode": "eip", "ip": "203.0.113.9", "prefix": 32}]
    assert "06:aa:bb:cc:dd:ee eip 203.0.113.9/32" in guestfix.netfix_map(nm)
    ps1 = guestfix.win_eip_ps1(nm)
    assert '$mac = "06-AA-BB-CC-DD-EE"' in ps1 and '$eips = @("203.0.113.9")' in ps1
    public.attach_eip(st, ex)
    public.attach_eip(st, ex)
    assert [c for c in ex.calls if c[0] == "attach"] == [("attach", "v2v-web01", "203.0.113.9")]
    public.release_eip(st, ex)                                     # kept by default
    assert "203.0.113.9" in ex.eip_table
    public.release_eip(st, ex, delete=True)
    assert ("detach", "v2v-web01", "203.0.113.9") in ex.calls and "203.0.113.9" not in ex.eip_table


def test_interrupted_create_reuses_its_elastic_ip(tmp_path, monkeypatch):
    ex = FakeExo()
    ex.eip_table["203.0.113.7"] = {"ip_address": "203.0.113.7", "id": "e7", "instances": [], "description": "v2v-web01"}
    st = mig(tmp_path, monkeypatch, cli.public_of("new"))
    public.reserve_eip(st, ex)
    assert st["receiver"]["eip"]["ip"] == "203.0.113.7" and not [c for c in ex.calls if c[0] == "eip_create"]


def test_exo_eip_create_survives_the_cli_reverse_dns_error(monkeypatch):
    from exo_v2v import exo
    e = exo.Exo.__new__(exo.Exo)
    e.zone, e.account, e.dry_run = "ch-gva-2", None, False
    table = []

    def run(*args, **kw):
        if args[:3] == ("compute", "elastic-ip", "create"):
            table.append({"ip_address": "203.0.113.8", "description": args[-1]})
            raise ExoError("exo compute elastic-ip create…: error: unable to update elastic ip reverse dns: operation is nil")
        if args[:3] == ("compute", "elastic-ip", "list"):
            return table
        if args[:3] == ("compute", "elastic-ip", "show"):
            return {**table[0], "id": "e8"}
        raise AssertionError(args)
    monkeypatch.setattr(e, "run", run)
    assert e.eip_create("v2v-x")["id"] == "e8"
    monkeypatch.setattr(e, "run", lambda *a, **k: (_ for _ in ()).throw(ExoError("error: quota exceeded")))
    with pytest.raises(ExoError, match="quota"):
        e.eip_create("v2v-y")


def test_existing_elastic_ip_is_never_deleted(tmp_path, monkeypatch):
    ex = FakeExo()
    st = mig(tmp_path, monkeypatch, cli.public_of("203.0.113.5", "www.example.com"))
    public.reserve_eip(st, ex)
    assert st["receiver"]["eip"]["created"] is False
    public.release_eip(st, ex, delete=True)
    assert "203.0.113.5" in ex.eip_table
    ex.eip_table["203.0.113.5"]["instances"] = ["someone-else"]
    st["receiver"].pop("eip")
    with pytest.raises(ExoError, match="attached to someone-else"):
        public.reserve_eip(st, ex)
    errs = []
    cli.public_of("not-an-ip", errors=errs)
    assert errs


def test_reverse_dns_without_elastic_ip(tmp_path, monkeypatch):
    ex = FakeExo()
    st = mig(tmp_path, monkeypatch, cli.public_of(None, "mx.example.com"))
    public.set_reverse_dns(st, ex)
    assert ex.calls == [("ptr-instance", "v2v-web01", "mx.example.com")]


NICS = [{"label": "Network adapter 1", "mac": "00:50:56:00:00:01", "port_group": "front", "ips": []},
        {"label": "Network adapter 2", "mac": "00:50:56:00:00:02", "port_group": "back",
         "ips": [{"ip": "10.20.0.254", "origin": "manual"}]}]


def test_renumber_keeps_host_part():
    nets = {"front": {"source": "front", "type": "public"},
            "back": {"source": "back", "type": "private", "cidr": "10.20.0.0/24", "renumber": "10.30.0.0/24",
                     "exoscale_name": "v2v-shop-back"}}
    nics = [NICS[0], {**NICS[1], "ips": [{"ip": "10.20.0.21", "origin": "manual"}]}]
    out, errs = plan.network_map(nics, nets)
    assert errs == [] and out[1]["ip"] == "10.30.0.21" and out[1]["from"] == "10.20.0.21"
    out, errs = plan.network_map(NICS, nets)        # .254 stays .254: Exoscale's DHCP server, refused
    assert errs and "Exoscale keeps" in errs[0]
    nets["back"].pop("renumber")
    out, errs = plan.network_map(NICS, nets, {"back": "10.20.0.54"})          # per-VM override renumbers
    assert errs == [] and out[1]["ip"] == "10.20.0.54" and out[1]["from"] == "10.20.0.254"
    line = guestfix.netfix_map([{"mac": "0a:00:00:00:00:02", "mode": "static", "ip": "10.20.0.54",
                                 "prefix": 24, "nogw": True, "from": "10.20.0.254"}])
    assert "0a:00:00:00:00:02 static 10.20.0.54/24 nogw from=10.20.0.254" in line


def manifest(**kw):
    m = {"name": "shop", "zone": "ch-gva-2",
         "networks": {"front": {"source": "front", "type": "public"},
                      "back": {"source": "back", "type": "private", "cidr": "10.20.0.0/24"}},
         "vms": [{"vm": "DC/vm/shop/lb01", "public": {"elastic_ip": "new", "reverse_dns": "shop.example.com"}},
                 {"vm": "db01"}]}
    m.update(kw)
    return m


def test_manifest_public_dns_renumber_rules():
    m = group.validate(manifest(dns={"records": {"www.example.com": "lb01"}},
                                security_groups={"db": ["tcp/5432 sg:app", "icmp 10.0.0.0/8"]}))
    assert m["dns"]["ttl"] == 60
    for bad in (dict(dns={"records": {"www.example.com": "web99"}}),
                dict(dns={"ttl": 60}),
                dict(elastic_ips=["new"]),                                    # unknown top-level key
                dict(security_groups={"db": ["tcp/5432 sg:bad name"]})):
        with pytest.raises(SystemExit):
            group.validate(manifest(**bad))
    m = manifest()
    m["vms"][1]["public"] = {"elastic_ip": "maybe"}
    with pytest.raises(SystemExit):
        group.validate(m)
    m = manifest()
    m["networks"]["back"]["renumber"] = "10.30.0.0/16"
    with pytest.raises(SystemExit):
        group.validate(m)
    assert group.parse_rule("tcp/5432 sg:app") == ("tcp", "5432", "sg:app")


def test_sync_rules_adds_missing_and_prunes_only_on_request():
    ex = FakeExo()
    ex.rules["db"] = [{"id": "r1", "protocol": "tcp", "start_port": 5432, "end_port": 5432, "security_group": "app"},
                      {"id": "r2", "protocol": "tcp", "start_port": 22, "end_port": 22, "network": "0.0.0.0/0"}]
    added, removed = group.sync_rules(ex, "db", ["tcp/5432 sg:app", "icmp 10.0.0.0/8"], "shop")
    assert added == ["icmp 10.0.0.0/8"] and removed == []
    run = [c for c in ex.calls if c[0] == "run"][0]
    assert "--network" in run and "--icmp-type" in run
    added, removed = group.sync_rules(ex, "db", ["tcp/5432 sg:app", "tcp/80-81 sg:lb"], "shop", prune=True)
    run = [c for c in ex.calls if c[0] == "run"][-1]
    assert run[run.index("--security-group") + 1] == "lb" and run[run.index("--port") + 1] == "80-81"
    assert ("rule-delete", "db", "r2") in ex.calls
