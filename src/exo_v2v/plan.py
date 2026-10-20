"""Migration plan: compatibility checks + Exoscale sizing from a vSphere inventory."""
from .exo import BIOS_TEMPLATE, UEFI_TEMPLATE
from .util import gib_ceil

# Exoscale "standard" family: (name, vCPU, RAM GiB)
STANDARD_TYPES = [("tiny", 1, 1), ("small", 1, 2), ("medium", 2, 4), ("large", 4, 8),
                  ("extra-large", 4, 16), ("huge", 8, 32), ("mega", 12, 64), ("titan", 16, 128),
                  ("jumbo", 24, 225)]
# Conversion runs virt-v2v (nested KVM + libguestfs appliance) inside the RAM
# root: needs >= 8 GiB RAM and nested virtualization (verified on standard.large).
CONVERT_MIN = "standard.large"
MAX_DISKS = 6            # 1 root disk + 5 Block Storage volumes
MIN_DISK_GB = 10


def instance_type_for(vcpus, mem_mb):
    gib = mem_mb / 1024
    for name, c, m in STANDARD_TYPES:
        if c >= vcpus and m >= gib:
            return f"standard.{name}"
    return f"standard.{STANDARD_TYPES[-1][0]}"


def _rank(itype):
    names = [n for n, _c, _m in STANDARD_TYPES]
    return names.index(itype.split(".", 1)[1]) if itype.startswith("standard.") and \
        itype.split(".", 1)[1] in names else len(names)


def os_family(guest_id):
    g = (guest_id or "").lower()
    if g.startswith("win"):
        return "windows"
    return "linux" if g else "unknown"


def build(inv, zone, target_type=None, system_disk=None, security_groups=(), receiver_template=None,
          receiver_template_visibility="public", receiver_template_boot_mode=None):
    errors, warnings, notes = [], [], []
    disks = inv["disks"]
    if not disks:
        errors.append("VM has no virtual disks")
    if len(disks) > MAX_DISKS:
        errors.append(f"{len(disks)} disks: Exoscale allows 1 root disk + 5 Block Storage volumes")
    for d in disks:
        if d["mode"].startswith("independent"):
            errors.append(f"{d['id']} is {d['mode']}: independent disks are excluded from snapshots, "
                          "so CBT cannot replicate them (switch to dependent/persistent)")
        if not d["file"] or not d["file"].endswith(".vmdk"):
            errors.append(f"{d['id']}: unsupported backing {d['file']!r} (RDM?)")
    sysd = system_disk or (disks[0]["id"] if disks else None)
    if disks and sysd not in [d["id"] for d in disks]:
        errors.append(f"system disk {sysd} not on the VM")
    if inv["firmware"] not in ("bios", "efi"):
        errors.append(f"unknown firmware {inv['firmware']}")
    secure_boot = bool(inv.get("secure_boot")) and inv["firmware"] == "efi"
    if secure_boot:
        notes.append("Secure Boot is on at the source: the instance is created with Secure Boot "
                     "(Exoscale sets it at creation only)")
    if inv.get("vtpm"):
        notes.append("the source has a vTPM: the instance is created with a TPM; secrets sealed to "
                     "the VMware vTPM (BitLocker, LUKS TPM unlock) do not move with it")
    if not inv["cbt_enabled"]:
        notes.append("CBT is off: `exo-v2v sync` enables it (a snapshot cycle activates it without downtime)")
    foreign = [n for n in inv["snapshots"] if not n.startswith("v2v-sync")]   # ours are transient
    if foreign:
        warnings.append(f"existing snapshots {foreign}: replication works, but consolidate them first if you can")
    if len(inv["nics"]) > 1:
        warnings.append(f"{len(inv['nics'])} NICs: the instance gets one public interface; "
                        "additional networks must be attached as Exoscale Private Networks afterwards")
    fam = os_family(inv["guest_id"])
    if fam == "windows":
        notes.append("Windows: `create` stages virtio-win + rhsrvany on the receiver (needs internet egress from the receiver)")
    if fam == "unknown":
        warnings.append(f"guest OS unknown ({inv['guest_id']}); conversion relies on virt-v2v inspection")

    target = target_type or instance_type_for(inv["cpus"], inv["memory_mb"])
    convert_type = target if _rank(target) >= _rank(CONVERT_MIN) else CONVERT_MIN
    if convert_type != target:
        notes.append(f"receiver runs as {convert_type} for conversion; scaled to {target} at cutover")

    if receiver_template:
        # exo-v2v receiver template: boots straight into the RAM-root receiver
        receiver_mode, template, visibility, receiver_user = \
            "template", receiver_template, receiver_template_visibility, "v2v"
        want = "uefi" if inv["firmware"] == "efi" else "legacy"
        if receiver_template_boot_mode and receiver_template_boot_mode != want:
            errors.append(f"receiver template {receiver_template!r} boots {receiver_template_boot_mode}, "
                          f"the VM needs {want}: use the {want} variant")
    else:
        # stock template + bootstrap that rebuilds itself into RAM and kexecs
        receiver_mode, visibility = "kexec", "public"
        template = UEFI_TEMPLATE if inv["firmware"] == "efi" else BIOS_TEMPLATE
        receiver_user = "ubuntu" if inv["firmware"] == "efi" else "debian"

    ordered = [d for d in disks if d["id"] == sysd] + [d for d in disks if d["id"] != sysd]
    mapping, volumes = [], []
    for i, d in enumerate(ordered):
        # volumes get >= 1 MiB slack: a GPT rescaled to 4096-byte sectors needs
        # 20 KiB at the end for its backup (16.5 KiB at 512), and a last partition
        # that reaches the last 512-byte usable LBA would otherwise overlap it
        slack = (1 << 20) if i else 0
        gb = max(MIN_DISK_GB, gib_ceil(d["capacity"] + slack))
        export = "sd" + chr(ord("a") + i)
        mapping.append({"disk": d["id"], "export": export, "capacity": d["capacity"], "size_gb": gb,
                        "role": "root" if i == 0 else "volume", "label": d["label"]})
        if i:
            volumes.append(gb)
    return {
        "vm": inv["name"], "zone": zone, "os_family": fam, "guest_id": inv["guest_id"],
        "firmware": inv["firmware"], "secure_boot": secure_boot, "tpm": bool(inv.get("vtpm")),
        "receiver_mode": receiver_mode, "template": template, "template_visibility": visibility,
        "receiver_user": receiver_user,
        "target_type": target, "convert_type": convert_type,
        "root_gb": mapping[0]["size_gb"] if mapping else 0, "volumes_gb": volumes,
        "disks": mapping, "security_groups": list(security_groups),
        "errors": errors, "warnings": warnings, "notes": notes,
    }
