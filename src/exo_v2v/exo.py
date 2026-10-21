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
                        ssh_keys=(), secureboot=False, tpm=False, template_visibility="public"):
        args = ["compute", "instance", "create", name, "-z", self.zone, "--template", template,
                "--instance-type", itype, "--disk-size", str(disk_gb),
                "--cloud-init", user_data, "--cloud-init-compress"]
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

    def instance_delete(self, name):
        return self.run("compute", "instance", "delete", name, "-z", self.zone, "-f", json_out=False)

    def instance_sg_add(self, name, sg):
        return self.run("compute", "instance", "security-group", "add", name, sg, "-z", self.zone, json_out=False)

    def instance_sg_remove(self, name, sg):
        return self.run("compute", "instance", "security-group", "remove", name, sg, "-z", self.zone, json_out=False)

    # --- block storage ---
    def volume_create(self, name, size_gb, labels):
        args = ["compute", "block-storage", "create", name, "-z", self.zone, "--size", str(size_gb)]
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

    def volume_delete(self, name):
        return self.run("compute", "block-storage", "delete", name, "-z", self.zone, "-f", json_out=False)

    def volume_label(self, name, labels):
        args = ["compute", "block-storage", "update", name, "-z", self.zone]
        for k, v in labels.items():
            args += ["--label", f"{k}={v}"]
        return self.run(*args, json_out=False)

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
