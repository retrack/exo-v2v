"""Thin wrapper around the `exo` CLI (JSON output).

The CLI carries the credentials (`exo config`, or EXOSCALE_API_KEY /
EXOSCALE_API_SECRET in the environment) so exo-v2v never handles API secrets.
"""
import json
import shutil
import subprocess

from .util import log

BIOS_TEMPLATE = "Linux Debian 13 (Trixie) 64-bit"     # boot mode: legacy
UEFI_TEMPLATE = "Linux Ubuntu 24.04 LTS 64-bit"       # boot mode: uefi


class ExoError(RuntimeError):
    pass


class Exo:
    def __init__(self, zone, account=None, dry_run=False):
        if not shutil.which("exo"):
            raise ExoError("the `exo` CLI is not installed (https://github.com/exoscale/cli)")
        self.zone, self.account, self.dry_run = zone, account, dry_run

    def run(self, *args, json_out=True, check=True, stdin=None):
        cmd = ["exo", *args]
        if self.account:
            cmd += ["--use-account", self.account]
        if json_out:
            cmd += ["-O", "json"]
        if self.dry_run and args[1] not in ("show", "list"):
            log(f"dry-run: {' '.join(cmd)}")
            return {}
        r = subprocess.run(cmd, capture_output=True, text=True, input=stdin)
        if r.returncode and check:
            raise ExoError(f"{' '.join(cmd[:4])}…: {(r.stderr or r.stdout).strip()[-400:]}")
        if not json_out:
            return r.stdout
        try:
            return json.loads(r.stdout) if r.stdout.strip() else {}
        except json.JSONDecodeError:
            return {"raw": r.stdout}

    # --- instances ---
    def instance_create(self, name, template, itype, disk_gb, security_groups, user_data, labels,
                        ssh_keys=(), secureboot=False, tpm=False, template_visibility="public",
                        anti_affinity_groups=(), public_ip=None, private_networks=()):
        args = ["compute", "instance", "create", name, "-z", self.zone, "--template", template,
                "--instance-type", itype, "--disk-size", str(disk_gb),
                "--cloud-init", user_data, "--cloud-init-compress"]
        if public_ip:                       # "none": a private instance (create-time only)
            args += ["--public-ip", public_ip]
        for n in private_networks:          # a private instance needs one to have any NIC
            args += ["--private-network", n]
        for g in anti_affinity_groups:      # create-time only on Exoscale
            args += ["--anti-affinity-group", g]
        if template_visibility != "public":
            args += ["--template-visibility", template_visibility]
        if secureboot:
            args.append("--secureboot")
        if tpm:
            args.append("--tpm")
        for sg in security_groups:
            args += ["--security-group", sg]
        for k in ssh_keys:
            args += ["--ssh-key", k]
        for k, v in labels.items():
            args += ["--label", f"{k}={v}"]
        try:
            return self.run(*args)
        except ExoError:
            self._drop_temp_ssh_keys(name)
            raise

    def _drop_temp_ssh_keys(self, instance_name):
        # `exo compute instance create` registers a temporary key `<instance>-<epoch>` and only
        # deletes it when the instance is created: remove the one a failed create leaves behind.
        try:
            keys = self.run("compute", "ssh-key", "list")
        except ExoError:
            return
        for k in keys if isinstance(keys, list) else []:
            n = k.get("name", "")
            if n.startswith(f"{instance_name}-") and n[len(instance_name) + 1:].isdigit():
                self.run("compute", "ssh-key", "delete", n, "-f", json_out=False, check=False)
                log(f"deleted temporary SSH key {n} left by the failed create")

    def instance(self, name):
        return self.run("compute", "instance", "show", name, "-z", self.zone)

    def instance_exists(self, name):
        try:
            self.instance(name)
            return True
        except ExoError:
            return False

    def instance_stop(self, name):
        return self.run("compute", "instance", "stop", name, "-z", self.zone, "-f", json_out=False)

    def instance_start(self, name):
        return self.run("compute", "instance", "start", name, "-z", self.zone, "-f", json_out=False)

    def instance_scale(self, name, itype):
        return self.run("compute", "instance", "scale", name, itype, "-z", self.zone, "-f", json_out=False)

    def instance_reset(self, name, template, disk_gb, template_visibility="public"):
        """Re-image the root disk from `template` at `disk_gb` (wipes it; volumes stay)."""
        args = ["compute", "instance", "reset", name, "-z", self.zone, "--template", template,
                "--disk-size", str(disk_gb), "-f"]
        if template_visibility != "public":
            args += ["--template-visibility", template_visibility]
        return self.run(*args, json_out=False)

    def instance_nics(self, name):
        """[{"network": "public" | private network id, "mac"}] in the order the
        instance sees them (the CLI's `instance show` has no MACs; the API does)."""
        raw = self.run("x", "get-instance", self.instance(name)["id"], "--zone", self.zone, json_out=False)
        d = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
        return ([{"network": "public", "mac": d["mac-address"]}] if d.get("mac-address") else []) + \
            [{"network": p["id"], "mac": p["mac-address"]} for p in d.get("private-networks", [])]

    def instance_rename(self, name, new):
        return self.run("compute", "instance", "update", name, "-z", self.zone, "--name", new, json_out=False)

    def instances(self):
        return self.run("compute", "instance", "list", "-z", self.zone) or []

    def instance_delete(self, name):
        return self.run("compute", "instance", "delete", name, "-z", self.zone, "-f", json_out=False)

    def instance_sg_add(self, name, sg):
        return self.run("compute", "instance", "security-group", "add", name, sg, "-z", self.zone, json_out=False)

    def instance_sg_remove(self, name, sg):
        return self.run("compute", "instance", "security-group", "remove", name, sg, "-z", self.zone, json_out=False)

    # --- block storage ---
    def volume_create(self, name, size_gb, labels, snapshot=None):
        args = ["compute", "block-storage", "create", name, "-z", self.zone, "--size", str(size_gb)]
        if snapshot:
            args += ["--snapshot", snapshot]
        for k, v in labels.items():
            args += ["--label", f"{k}={v}"]
        return self.run(*args)

    def volume(self, name):
        return self.run("compute", "block-storage", "show", name, "-z", self.zone)

    def volumes(self):
        return self.run("compute", "block-storage", "list", "-z", self.zone) or []

    def volume_attach(self, name, instance):
        return self.run("compute", "block-storage", "attach", name, instance, "-z", self.zone, json_out=False)

    def volume_detach(self, name):
        return self.run("compute", "block-storage", "detach", name, "-z", self.zone, "-f", json_out=False)

    def volume_resize(self, name, size_gb):
        """Grow a volume (Exoscale: only while detached)."""
        return self.run("compute", "block-storage", "update", name, "-z", self.zone, "--size", str(size_gb),
                        json_out=False)

    def snapshot_create(self, volume, name, labels):
        args = ["compute", "block-storage", "snapshot", "create", volume, "-z", self.zone, "--name", name]
        for k, v in labels.items():
            args += ["--label", f"{k}={v}"]
        return self.run(*args)

    def snapshot_delete(self, name):
        return self.run("compute", "block-storage", "snapshot", "delete", name, "-z", self.zone, "-f",
                        json_out=False)

    def snapshots(self):
        return self.run("compute", "block-storage", "snapshot", "list", "-z", self.zone) or []

    def volume_delete(self, name):
        return self.run("compute", "block-storage", "delete", name, "-z", self.zone, "-f", json_out=False)

    def volume_label(self, name, labels):
        args = ["compute", "block-storage", "update", name, "-z", self.zone]
        for k, v in labels.items():
            args += ["--label", f"{k}={v}"]
        return self.run(*args, json_out=False)

    # --- private networks / anti-affinity groups ---
    def privnet(self, name):
        return self.run("compute", "private-network", "show", name, "-z", self.zone)

    def privnet_create(self, name, start_ip, end_ip, netmask, description="exo-v2v", dns_servers=()):
        args = ["compute", "private-network", "create", name, "-z", self.zone, "--start-ip", start_ip,
                "--end-ip", end_ip, "--netmask", netmask, "--description", description]
        for d in dns_servers:               # DHCP option 6
            args += ["--dns-server", d]
        return self.run(*args)

    def privnet_leases(self, name):
        """{instance name: address} of a managed private network."""
        return {x["instance"]: x["ip_address"] for x in self.privnet(name).get("leases") or []}

    def privnet_attach(self, instance, network, ip=None):
        args = ["compute", "instance", "private-network", "attach", instance, network, "-z", self.zone]
        if ip:
            args += ["--ip", ip]            # static lease (managed private networks)
        return self.run(*args, json_out=False)

    def privnet_delete(self, name):
        return self.run("compute", "private-network", "delete", name, "-z", self.zone, "-f", json_out=False)

    def aag(self, name):
        return self.run("compute", "anti-affinity-group", "show", name)

    def aag_create(self, name):
        return self.run("compute", "anti-affinity-group", "create", name, "--description", "exo-v2v")

    def aag_delete(self, name):
        return self.run("compute", "anti-affinity-group", "delete", name, "-f", json_out=False)

    def exists(self, kind, name):
        try:
            {"privnet": self.privnet, "aag": self.aag, "sg": self.sg, "instance": self.instance}[kind](name)
            return True
        except ExoError:
            return False

    # --- security groups ---
    def sg_create(self, name):
        return self.run("compute", "security-group", "create", name)

    def sg(self, name):
        return self.run("compute", "security-group", "show", name)

    def sg_rule_add(self, sg, cidr, port, proto="tcp", desc=""):
        return self.run("compute", "security-group", "rule", "add", sg, "--network", cidr,
                        "--protocol", proto, "--port", str(port), "--description", desc or "exo-v2v",
                        json_out=False)

    def sg_delete(self, name):
        return self.run("compute", "security-group", "delete", name, "-f", json_out=False)

    def template_boot_mode(self, name, visibility="public"):
        t = self.run("compute", "instance-template", "show", name, "-z", self.zone,
                     "--visibility", visibility)
        return t.get("boot_mode", "legacy")


def volume_size_gb(v):
    """'10 GiB' -> 10."""
    s = v.get("size", 0)
    return int(str(s).split()[0]) if s else 0
