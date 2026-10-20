#!/usr/bin/env bash
# test_netfix.sh — the first-boot network fix against fake guest trees (no root needed).
# Scenarios = what real VMware guests carry (seen in the T3b run of 2026-10-16):
#   debian:  ifupdown "allow-hotplug ens224" (vmxnet3 name) -> NIC is ens3 on Exoscale
#   ubuntu:  netplan ethernets.ens192 + macaddress pin + 70-persistent-net rule
#   rhel:    ifcfg-ens192 with HWADDR + NM keyfile interface-name=ens192 mac-address
#   ok:      config already names the present NIC -> untouched
#   none:    no config at all -> fallback DHCP stanza
set -uo pipefail
HERE="$(cd "$(dirname "$0")/../src/exo_v2v/receiver" && pwd)"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
python3 -I -c "import sys; sys.path.insert(0, '$HERE'); import guestfix; open('$W/netfix','w').write(guestfix.NETFIX_SH)"
fail=0; ok() { echo "  ok: $*"; }; bad() { echo "  FAIL: $*"; fail=1; }
nic() { mkdir -p "$1/sys/class/net/$2/device"; }
run() { V2V_ROOT="$1/root" V2V_SYS="$1/sys" sh "$W/netfix"; }

echo "== debian"; T=$W/deb; mkdir -p $T/root/etc/network/interfaces.d; nic $T ens3; mkdir -p $T/sys/class/net/lo
cat > $T/root/etc/network/interfaces <<'EOF'
source /etc/network/interfaces.d/*
auto lo
iface lo inet loopback
allow-hotplug ens224
iface ens224 inet dhcp
EOF
run $T
grep -q '^auto ens3$' $T/root/etc/network/interfaces && ok "allow-hotplug ens224 -> auto ens3" || bad "$(cat $T/root/etc/network/interfaces)"
grep -q '^iface ens3 inet dhcp$' $T/root/etc/network/interfaces && ok "iface renamed" || bad "iface"
grep -q 'ens224' $T/root/etc/network/interfaces && bad "stale name left" || ok "no stale name"
cp $T/root/etc/network/interfaces $W/once; run $T; cmp -s $W/once $T/root/etc/network/interfaces && ok "idempotent" || bad "second run changed config"

echo "== ubuntu"; T=$W/ubu; mkdir -p $T/root/etc/netplan $T/root/etc/udev/rules.d; nic $T ens3
cat > $T/root/etc/netplan/50-vmware.yaml <<'EOF'
network:
  version: 2
  ethernets:
    ens192:
      match:
        macaddress: "00:50:56:aa:bb:cc"
      dhcp4: true
EOF
echo 'SUBSYSTEM=="net", ATTR{address}=="00:50:56:aa:bb:cc", NAME="ens192"' > $T/root/etc/udev/rules.d/70-persistent-net.rules
run $T
grep -q '^    ens3:' $T/root/etc/netplan/50-vmware.yaml && ok "netplan ens192 -> ens3" || bad "$(cat $T/root/etc/netplan/50-vmware.yaml)"
grep -qi '00:50:56' $T/root/etc/netplan/50-vmware.yaml && bad "MAC pin left" || ok "MAC pin removed"
grep -q 'match:' $T/root/etc/netplan/50-vmware.yaml && bad "empty match: left" || ok "empty match: dropped"
python3 -c "import yaml,sys; d=yaml.safe_load(open(sys.argv[1])); assert d['network']['ethernets']['ens3']['dhcp4'] is True" $T/root/etc/netplan/50-vmware.yaml && ok "netplan YAML valid, ens3 dhcp4" || bad "netplan YAML broken: $(cat $T/root/etc/netplan/50-vmware.yaml)"
[ -f $T/root/etc/udev/rules.d/70-persistent-net.rules ] && bad "udev rule kept" || ok "VMware udev rule disabled"

echo "== rhel"; T=$W/rh; mkdir -p $T/root/etc/sysconfig/network-scripts $T/root/etc/NetworkManager/system-connections; nic $T eth0
printf 'DEVICE=ens192\nHWADDR=00:0C:29:11:22:33\nBOOTPROTO=dhcp\nONBOOT=yes\n' > $T/root/etc/sysconfig/network-scripts/ifcfg-ens192
printf '[connection]\nid=ens192\ntype=ethernet\ninterface-name=ens192\n[ethernet]\nmac-address=00:0C:29:11:22:33\n[ipv4]\nmethod=auto\n' > $T/root/etc/NetworkManager/system-connections/ens192.nmconnection
run $T
[ -f $T/root/etc/sysconfig/network-scripts/ifcfg-eth0 ] && grep -q '^DEVICE=eth0' $T/root/etc/sysconfig/network-scripts/ifcfg-eth0 && ok "ifcfg-ens192 -> ifcfg-eth0" || bad "ifcfg: $(ls $T/root/etc/sysconfig/network-scripts)"
grep -qi HWADDR $T/root/etc/sysconfig/network-scripts/ifcfg-eth0 2>/dev/null && bad "HWADDR left" || ok "HWADDR removed"
grep -q '^interface-name=eth0$' $T/root/etc/NetworkManager/system-connections/ens192.nmconnection && ok "NM interface-name -> eth0" || bad "NM: $(cat $T/root/etc/NetworkManager/system-connections/ens192.nmconnection)"
grep -qi 'mac-address' $T/root/etc/NetworkManager/system-connections/ens192.nmconnection && bad "NM mac pin left" || ok "NM mac pin removed"

echo "== ok (already right)"; T=$W/ok; mkdir -p $T/root/etc/network; nic $T ens3
printf 'auto lo\niface lo inet loopback\nauto ens3\niface ens3 inet dhcp\n' > $T/root/etc/network/interfaces; cp $T/root/etc/network/interfaces $W/ok0
run $T; cmp -s $W/ok0 $T/root/etc/network/interfaces && ok "untouched" || bad "modified a correct config"

echo "== none (no config)"; T=$W/none; mkdir -p $T/root/etc/network; nic $T ens3
printf 'auto lo\niface lo inet loopback\n' > $T/root/etc/network/interfaces
run $T; grep -q 'iface ens3 inet dhcp' $T/root/etc/network/interfaces.d/v2v-ens3 2>/dev/null && ok "fallback DHCP stanza" || bad "no fallback"

[ $fail = 0 ] && echo PASS || { echo FAILED; exit 1; }
