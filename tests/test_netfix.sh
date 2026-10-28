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

# ---- plan-driven map (group members: public NIC + private network with the same address) ----
A=06:a1:02:00:07:cd; B=0a:e2:d0:88:6e:d6
nicm() { nic "$1" "$2"; echo "$3" > "$1/sys/class/net/$2/address"; }
map() { mkdir -p "$1/root/etc"; printf '%s dhcp\n%s static %s nogw\n' "$A" "$B" "$2" > "$1/root/etc/v2v-netfix.map"; }

echo "== map: rhel NM keyfile static (app/db server)"; T=$W/mrh; K=$T/root/etc/NetworkManager/system-connections
mkdir -p $K; nicm $T eth0 $A; nicm $T eth1 $B; map $T 10.20.0.31/24
printf '[connection]\nid=ens192\ntype=ethernet\ninterface-name=ens192\n[ethernet]\nmac-address=00:50:56:01:02:03\n[ipv4]\nmethod=manual\naddress1=10.20.0.31/24,10.20.0.1\ndns=10.20.0.1;\n' > $K/ens192.nmconnection
run $T
grep -q '^interface-name=eth1$' $K/ens192.nmconnection && ok "static profile -> eth1 (private NIC, by MAC)" || bad "$(cat $K/ens192.nmconnection)"
grep -q '^address1=10.20.0.31/24$' $K/ens192.nmconnection && ok "gateway stripped from address1" || bad "address1: $(grep address1 $K/ens192.nmconnection)"
grep -q '^never-default=true$' $K/ens192.nmconnection && ok "never-default" || bad "no never-default"
grep -q '^interface-name=eth0$' $K/v2v-eth0.nmconnection 2>/dev/null && grep -q '^method=auto' $K/v2v-eth0.nmconnection && ok "DHCP profile for eth0 (public)" || bad "no DHCP profile: $(ls $K)"
cp -r $K $W/mrh1; run $T; diff -r $W/mrh1 $K >/dev/null && ok "idempotent" || bad "second run changed: $(diff -r $W/mrh1 $K)"

echo "== map: debian ifupdown, 2 NICs (load balancer)"; T=$W/mdeb; mkdir -p $T/root/etc/network/interfaces.d; nicm $T ens3 $A; nicm $T ens4 $B; map $T 10.20.0.10/24
cat > $T/root/etc/network/interfaces <<'EOF'
source /etc/network/interfaces.d/*
auto lo
iface lo inet loopback
allow-hotplug ens192
iface ens192 inet dhcp
allow-hotplug ens224
iface ens224 inet static
    address 10.20.0.10/24
    gateway 10.20.0.1
EOF
run $T; F=$T/root/etc/network/interfaces
awk '/^iface ens4/{s=1} s&&/address 10.20.0.10/{print "y"; exit}' $F | grep -q y && ok "static stanza -> ens4 (by MAC)" || bad "$(cat $F)"
grep -q '^iface ens3 inet dhcp$' $F && ok "dhcp stanza -> ens3 (public)" || bad "dhcp: $(cat $F)"
grep -q gateway $F && bad "gateway kept on the private NIC" || ok "gateway dropped"
grep -qE 'ens192|ens224' $F && bad "stale names left" || ok "no stale names"
cp $F $W/mdeb1; run $T; cmp -s $W/mdeb1 $F && ok "idempotent" || bad "second run changed config"

echo "== map: ubuntu netplan static with default route"; T=$W/mubu; mkdir -p $T/root/etc/netplan; nicm $T ens3 $A; nicm $T ens4 $B; map $T 10.20.0.21/24
cat > $T/root/etc/netplan/50-cloud-init.yaml <<'EOF'
network:
  version: 2
  ethernets:
    ens192:
      addresses:
        - 10.20.0.21/24
      routes:
        - to: default
          via: 10.20.0.1
      nameservers:
        addresses: [10.20.0.1]
EOF
run $T; F=$T/root/etc/netplan/50-cloud-init.yaml
python3 - $F $T/root/etc/netplan/90-v2v-ens3.yaml <<'EOF' && ok "netplan: static -> ens4 without default route, DHCP file for ens3" || bad "netplan: $(cat $F; cat $T/root/etc/netplan/90-v2v-ens3.yaml)"
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))["network"]["ethernets"]
assert list(d) == ["ens4"] and d["ens4"]["addresses"] == ["10.20.0.21/24"] and "routes" not in d["ens4"], d
assert d["ens4"]["nameservers"]["addresses"] == ["10.20.0.1"], d
assert yaml.safe_load(open(sys.argv[2]))["network"]["ethernets"]["ens3"]["dhcp4"] is True
EOF

echo "== map: eth0 naming swap (static eth0 -> eth1, public eth0 gets DHCP)"; T=$W/mswap; S=$T/root/etc/sysconfig
mkdir -p $S/network-scripts; nicm $T eth0 $A; nicm $T eth1 $B; map $T 10.20.0.31/24
printf 'DEVICE=eth0\nHWADDR=00:50:56:01:02:03\nBOOTPROTO=none\nIPADDR=10.20.0.31\nPREFIX=24\nGATEWAY=10.20.0.1\nONBOOT=yes\n' > $S/network-scripts/ifcfg-eth0
printf 'NETWORKING=yes\nGATEWAY=10.20.0.1\n' > $S/network
run $T
grep -q '^IPADDR=10.20.0.31' $S/network-scripts/ifcfg-eth1 2>/dev/null && grep -q '^DEVICE=eth1' $S/network-scripts/ifcfg-eth1 && ok "ifcfg-eth0 static -> ifcfg-eth1" || bad "$(ls $S/network-scripts; cat $S/network-scripts/*)"
grep -q '^DEFROUTE=no' $S/network-scripts/ifcfg-eth1 && ! grep -q GATEWAY $S/network-scripts/ifcfg-eth1 $S/network && ok "no gateway, DEFROUTE=no" || bad "gateway: $(cat $S/network-scripts/ifcfg-eth1 $S/network)"
grep -q '^BOOTPROTO=dhcp' $S/network-scripts/ifcfg-eth0 2>/dev/null && ok "ifcfg-eth0 DHCP (public)" || bad "eth0: $(cat $S/network-scripts/ifcfg-eth0 2>&1)"

# ---- 0.6: renumbering (from=OLD) and Elastic IP lines ----
remap() { mkdir -p "$1/root/etc"; printf '%s dhcp\n%s eip 203.0.113.3/32\n%s static %s nogw from=%s\n' "$A" "$A" "$B" "$2" "$3" > "$1/root/etc/v2v-netfix.map"; }

echo "== renumber: debian ifupdown 10.20.0.254 -> 10.20.0.54"; T=$W/rdeb; mkdir -p $T/root/etc/network/interfaces.d; nicm $T ens3 $A; nicm $T ens4 $B
remap $T 10.20.0.54/24 10.20.0.254
cat > $T/root/etc/network/interfaces <<'EOF'
source /etc/network/interfaces.d/*
auto lo
iface lo inet loopback
allow-hotplug ens192
iface ens192 inet dhcp
allow-hotplug ens224
iface ens224 inet static
    address 10.20.0.254/24
EOF
printf '127.0.0.1 localhost\n10.20.0.254 db01.lan db01\n10.20.0.2540 not-this\n' > $T/root/etc/hosts
run $T; F=$T/root/etc/network/interfaces
awk '/^iface ens4/{s=1} s&&/address 10.20.0.54\/24/{print "y"; exit}' $F | grep -q y && ok "address renumbered on ens4" || bad "$(cat $F)"
grep -q '10.20.0.254/' $F && bad "old address left" || ok "old address gone"
grep -q '^10.20.0.54 db01.lan db01$' $T/root/etc/hosts && grep -q '^10.20.0.2540 not-this$' $T/root/etc/hosts && ok "/etc/hosts: own name renumbered, longer address untouched" || bad "hosts: $(cat $T/root/etc/hosts)"
grep -q '^iface ens3 inet dhcp$' $F && ! ls $T/root/etc/network/interfaces.d/ | grep -q ens3 && ok "eip line: public NIC still the DHCP one, no extra config" || bad "public: $(cat $F; ls $T/root/etc/network/interfaces.d)"
cp $F $W/rdeb1; run $T; cmp -s $W/rdeb1 $F && ok "idempotent" || bad "second run changed config"

echo "== renumber: netplan, NM keyfile, ifcfg, networkd"; T=$W/rmix; mkdir -p $T/root/etc/netplan; nicm $T ens3 $A; nicm $T ens4 $B
remap $T 172.16.5.21/24 10.20.0.21
printf 'network:\n  version: 2\n  ethernets:\n    ens192:\n      addresses:\n        - 10.20.0.21/24\n' > $T/root/etc/netplan/50-cloud-init.yaml
run $T; python3 -c "import yaml,sys; d=yaml.safe_load(open(sys.argv[1]))['network']['ethernets']; assert d['ens4']['addresses']==['172.16.5.21/24'], d" $T/root/etc/netplan/50-cloud-init.yaml && ok "netplan renumbered on ens4" || bad "$(cat $T/root/etc/netplan/50-cloud-init.yaml)"
T=$W/rnm; K=$T/root/etc/NetworkManager/system-connections; mkdir -p $K; nicm $T eth0 $A; nicm $T eth1 $B; remap $T 172.16.5.31/24 10.20.0.31
printf '[connection]\nid=ens192\ntype=ethernet\ninterface-name=ens192\n[ipv4]\nmethod=manual\naddress1=10.20.0.31/24,10.20.0.1\n' > $K/ens192.nmconnection
run $T; grep -q '^address1=172.16.5.31/24$' $K/ens192.nmconnection && grep -q '^interface-name=eth1$' $K/ens192.nmconnection && ok "NM keyfile renumbered on eth1" || bad "$(cat $K/ens192.nmconnection)"
T=$W/rifcfg; S=$T/root/etc/sysconfig/network-scripts; mkdir -p $S; nicm $T eth0 $A; nicm $T eth1 $B; remap $T 172.16.5.41/24 10.20.0.41
printf 'DEVICE=ens192\nBOOTPROTO=none\nIPADDR=10.20.0.41\nPREFIX=24\nONBOOT=yes\n' > $S/ifcfg-ens192
run $T; grep -q '^IPADDR=172.16.5.41$' $S/ifcfg-eth1 2>/dev/null && ok "ifcfg renumbered on eth1" || bad "$(ls $S; cat $S/*)"
T=$W/rnd; N=$T/root/etc/systemd/network; mkdir -p $N; nicm $T ens3 $A; nicm $T ens4 $B; remap $T 172.16.5.51/24 10.20.0.51
printf '[Match]\nName=ens192\n[Network]\nAddress=10.20.0.51/24\n' > $N/10-lan.network
printf '[Match]\nName=ens160\n[Network]\nDHCP=yes\n' > $N/05-wan.network
run $T; grep -q '^Address=172.16.5.51/24$' $N/10-lan.network && ok "networkd renumbered" || bad "$(cat $N/*)"
grep -q '^Name=ens4$' $N/10-lan.network && grep -q '^Name=ens3$' $N/05-wan.network && ! ls $N | grep -q 90-v2v && ok "networkd: static -> ens4, DHCP -> ens3, nothing added" || bad "networkd names: $(head -n2 $N/*)"
printf '[Match]\nName=en*\n[Network]\nDHCP=yes\n' > $N/99-glob.network; run $T; grep -q '^Name=en\*$' $N/99-glob.network && ok "networkd glob Name= left alone" || bad "glob: $(cat $N/99-glob.network)"

[ $fail = 0 ] && echo PASS || { echo FAILED; exit 1; }
