#!/bin/sh
# Reproduce LabAgent OSPF faults in isolated Linux/FRRouting namespaces.
set -eu
umask 022
case_id=${1:?usage: run_b_frr_lab.sh lab_02|lab_03|lab_02_backup|lab_02_partial OUTPUT_DIRECTORY}
case "$case_id" in lab_02|lab_03|lab_02_backup|lab_02_partial) ;; *) echo 'Choose lab_02, lab_03, lab_02_backup or lab_02_partial.' >&2; exit 2;; esac
out=${2:?supply an absolute output directory}
case "$out" in /*) ;; *) echo 'Use an absolute output directory.' >&2; exit 2;; esac
[ "$(id -u)" = 0 ] || { echo 'Run as root in a Linux FRRouting lab.' >&2; exit 2; }
frrdir=''
for path in /usr/lib/frr /usr/libexec/frr; do
    if [ -x "$path/zebra" ] && [ -x "$path/ospfd" ]; then frrdir=$path; break; fi
done
[ -n "$frrdir" ] || { echo 'Install FRRouting with zebra and ospfd.' >&2; exit 2; }
if [ "$case_id" = lab_02_backup ] && { [ ! -x "$frrdir/staticd" ] || [ ! -x "$frrdir/mgmtd" ]; }; then
    echo 'Install FRRouting staticd and mgmtd for lab_02_backup.' >&2
    exit 2
fi
mkdir "$out"
tmp=$(mktemp -d /tmp/labagent-ospf.XXXXXX)
tag="labagent_${tmp##*.}"
r1="${tag}_r1"; r2="${tag}_r2"; pc1="${tag}_pc1"; pc2="${tag}_pc2"
created=''
cleanup() {
    for ns in $created; do
        for pid in $(ip netns pids "$ns"); do kill "$pid" 2>/dev/null || :; done
        ip netns del "$ns" 2>/dev/null || :
    done
    for router in "$r1" "$r2"; do
        rm -f "/run/frr/$router/ospfd.pid" "/run/frr/$router/zebra.pid" \
              "/run/frr/$router/ospfd.vty" "/run/frr/$router/zebra.vty" "/run/frr/$router/zserv.api" \
              "/run/frr/$router/staticd.pid" "/run/frr/$router/staticd.vty" \
              "/run/frr/$router/mgmtd.pid" "/run/frr/$router/mgmtd.vty" \
              "/run/frr/$router/mgmtd_fe.sock" "/run/frr/$router/mgmtd_be.sock"
        rmdir "/run/frr/$router" 2>/dev/null || :
    done
    rm -f "$tmp"/*-zebra.conf "$tmp"/*-ospfd.conf
    rmdir "$tmp"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

timestamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
printf '%s\n' "$case_id" > "$out/case.txt"
timestamp > "$out/started-at.txt"
uname -sr > "$out/platform.txt"
"$frrdir/ospfd" --version > "$out/frr-version.txt" 2>&1

for ns in "$r1" "$r2" "$pc1" "$pc2"; do
    ip netns add "$ns"
    created="$created $ns"
    ip -n "$ns" link set lo up
done
link_pair() {
    ip link add "$2" netns "$1" type veth peer name "$5" netns "$4"
    ip -n "$1" addr add "$3" dev "$2"
    ip -n "$4" addr add "$6" dev "$5"
    ip -n "$1" link set "$2" up
    ip -n "$4" link set "$5" up
}
link_pair "$pc1" eth0 192.168.10.10/24 "$r1" lan 192.168.10.1/24
link_pair "$r1" transit 10.12.0.1/24 "$r2" transit 10.12.0.2/24
link_pair "$r2" lan 192.168.20.1/24 "$pc2" eth0 192.168.20.10/24
ip -n "$pc1" route add default via 192.168.10.1
ip -n "$pc2" route add default via 192.168.20.1
for ns in "$r1" "$r2"; do
    ip netns exec "$ns" sysctl -w net.ipv4.ip_forward=1
done

start_router() {
    ns=$1; label=$2; rid=$3
    mkdir -p "/run/frr/$ns"
    chown frr:frr "/run/frr/$ns"
    printf 'hostname %s\n' "$label" > "$tmp/$label-zebra.conf"
    cat > "$tmp/$label-ospfd.conf" <<EOF
hostname $label
log stdout
interface transit
 ip ospf network point-to-point
 ip ospf hello-interval 1
 ip ospf dead-interval 4
 ip ospf area 0.0.0.0
!
interface lan
 ip ospf area 0.0.0.0
 ip ospf passive
!
router ospf
 ospf router-id $rid
!
EOF
    chmod 0755 "$tmp"
    ip netns exec "$ns" "$frrdir/zebra" -d -N "$ns" -f "$tmp/$label-zebra.conf" -A 127.0.0.1 >> "$out/frr-console.txt" 2>&1
    ip netns exec "$ns" "$frrdir/ospfd" -d -N "$ns" -f "$tmp/$label-ospfd.conf" -A 127.0.0.1 >> "$out/frr-console.txt" 2>&1
    if [ "$case_id" = lab_02_backup ]; then
        ip netns exec "$ns" "$frrdir/mgmtd" -d -N "$ns" -A 127.0.0.1 >> "$out/frr-console.txt" 2>&1
        ip netns exec "$ns" "$frrdir/staticd" -d -N "$ns" -A 127.0.0.1 >> "$out/frr-console.txt" 2>&1
    fi
}
start_router "$r1" R1 1.1.1.1
start_router "$r2" R2 2.2.2.2

vty() { ns=$1; shift; ip netns exec "$ns" vtysh -N "$ns" "$@" 2>> "$out/frr-console.txt"; }
if [ "$case_id" = lab_02_backup ]; then
    vty "$r1" -c 'configure terminal' -c 'ip route 192.168.20.0/24 10.12.0.2 200' -c 'end'
    vty "$r2" -c 'configure terminal' -c 'ip route 192.168.10.0/24 10.12.0.1 200' -c 'end'
fi
route_present() {
    ip -n "$1" route show exact "$2" | grep -Fq "${3:-via }"
}
wait_state() {
    expected=$1
    for attempt in $(seq 1 25); do
        if [ "$expected" = up ]; then
            if route_present "$r1" 192.168.20.0/24 "${2:-via }" && route_present "$r2" 192.168.10.0/24 "${2:-via }"; then return 0; fi
        else
            if ! route_present "$r1" 192.168.20.0/24 && ! route_present "$r2" 192.168.10.0/24; then return 0; fi
        fi
        sleep 1
    done
    printf 'Convergence check timed out: %s\n' "$expected" >&2
    return 1
}
capture_ping() {
    rc=0
    ip netns exec "$1" ping -n -c 5 -i 0.2 -W 1 -w 8 "$2" > "$3" 2>&1 || rc=$?
    printf '%s\n' "$rc" > "$3.exit"
}
capture() {
    stage=$1
    folder="$out/$stage"
    mkdir "$folder"
    timestamp > "$folder/captured-at.txt"
    for pair in "R1:$r1" "R2:$r2"; do
        label=${pair%%:*}; ns=${pair#*:}
        vty "$ns" -c 'show running-config' > "$folder/$label-config.txt"
        vty "$ns" -c 'show ip ospf neighbor' > "$folder/$label-neighbors.txt"
        vty "$ns" -c 'show ip ospf neighbor json' > "$folder/$label-neighbors.json"
        vty "$ns" -c 'show ip route ospf' > "$folder/$label-ospf-routes.txt"
        if [ "$case_id" = lab_02_backup ]; then
            vty "$ns" -c 'show ip route' > "$folder/$label-all-routes.txt"
        fi
        vty "$ns" -c 'show ip route json' > "$folder/$label-routes.json"
        ip -n "$ns" link show dev transit > "$folder/$label-transit-link.txt"
        ip -n "$ns" -br address show > "$folder/$label-addresses.txt"
        ip -n "$ns" route show table main > "$folder/$label-kernel-routes.txt"
    done
    ip -n "$pc1" -br address show > "$folder/PC1-addresses.txt"
    ip -n "$pc2" -br address show > "$folder/PC2-addresses.txt"
    ip -n "$pc1" route show table main > "$folder/PC1-routes.txt"
    ip -n "$pc2" route show table main > "$folder/PC2-routes.txt"
    capture_ping "$pc1" 192.168.20.10 "$folder/PC1-ping.txt"
    capture_ping "$pc2" 192.168.10.10 "$folder/PC2-ping.txt"
    capture_ping "$r1" 10.12.0.2 "$folder/R1-direct-ping.txt"
    printf 'Captured %s\n' "$stage"
}
change_area() {
    vty "$r2" -c 'configure terminal' -c 'interface transit' \
        -c "no ip ospf area $1" -c "ip ospf area $2" -c 'end'
}

primary_route='via '
if [ "$case_id" = lab_02_backup ]; then primary_route='proto ospf'; fi
wait_state up "$primary_route"
capture baseline
case "$case_id" in
    lab_02|lab_02_backup) change_area 0.0.0.0 0.0.0.1 ;;
    lab_03) ip -n "$r2" link set dev transit down ;;
    lab_02_partial)
        change_area 0.0.0.0 0.0.0.1
        ip -n "$r2" link set dev transit down ;;
esac > "$out/inject.txt" 2>&1
if [ "$case_id" = lab_02_backup ]; then wait_state up 'proto static'; else wait_state down; fi
capture fault
if [ "$case_id" = lab_02_partial ]; then
    ip -n "$r2" link set dev transit up > "$out/partial-repair.txt" 2>&1
    wait_state down
    capture partial
fi
case "$case_id" in
    lab_02|lab_02_backup|lab_02_partial) change_area 0.0.0.1 0.0.0.0 ;;
    lab_03) ip -n "$r2" link set dev transit up ;;
esac > "$out/repair.txt" 2>&1
wait_state up "$primary_route"
capture repaired
timestamp > "$out/completed-at.txt"
