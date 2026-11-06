#!/usr/bin/env bash
# Create a Debian 13 LXC on Proxmox VE and install Echo Hermes Bridge in it.
# Run it in the Proxmox host shell (as root):
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Naod6473/echo-hermes-bridge/main/deploy/proxmox-lxc.sh)"
#
# Every value can be preset with an environment variable (CTID, CT_HOSTNAME, CORES, RAM, DISK,
# STORAGE, TEMPLATE_STORAGE, BRIDGE, VLAN, ECHO_HOST, HERMES_URL, HERMES_API_KEY, LANGUAGE, WAKE_WORD).
# Nothing is created before you confirm the summary.
set -euo pipefail

INSTALLER_URL="${INSTALLER_URL:-https://raw.githubusercontent.com/Naod6473/echo-hermes-bridge/main/install.sh}"

bold=$'\e[1m'; dim=$'\e[2m'; green=$'\e[32m'; yellow=$'\e[33m'; red=$'\e[31m'; reset=$'\e[0m'
step() { printf '\n%s==>%s %s%s%s\n' "$green" "$reset" "$bold" "$*" "$reset"; }
die()  { printf '%sx%s %s\n' "$red" "$reset" "$*" >&2; exit 1; }
ask() {        # ask VAR "question" "default"
    local var=$1 q=$2 def=${3:-} ans
    if [[ -n "${!var:-}" ]]; then return; fi
    read -r -p "  $q${def:+ ${dim}[$def]${reset}}: " ans </dev/tty || true
    printf -v "$var" '%s' "${ans:-$def}"
}

cat <<EOF
${bold}
  ┌──────────────────────────────────────────┐
  │   Echo Hermes Bridge: Proxmox LXC        │
  └──────────────────────────────────────────┘${reset}
EOF

[[ $EUID -eq 0 ]] || die "Run me as root in the Proxmox host shell."
for c in pct pveam pvesh pvesm; do command -v $c >/dev/null || die "'$c' not found: run me on a Proxmox VE host."; done

step "Container settings"
ask CTID "Container ID" "$(pvesh get /cluster/nextid)"
pct status "$CTID" >/dev/null 2>&1 && die "CT $CTID already exists."
ask CT_HOSTNAME "Hostname" "hermes-echo"
ask CORES "CPU cores" "4"
ask RAM "RAM in MB" "2048"
ask DISK "Disk in GB" "8"
echo "  ${dim}storages for containers: $(pvesm status -content rootdir 2>/dev/null | awk 'NR>1{printf "%s ", $1}')${reset}"
ask STORAGE "Storage" "local-lvm"
ask TEMPLATE_STORAGE "Storage for the Debian template" "local"
ask BRIDGE "Network bridge" "vmbr0"
ask VLAN "VLAN tag (empty = none)" ""

step "Bridge settings"
ask LANGUAGE "Language (en, fr...)" "en"
ask ECHO_HOST "Your Echo's IP address" ""
ask HERMES_URL "Hermes API URL as seen from the new container (e.g. http://192.168.1.20:8642/v1)" ""
[[ -n $HERMES_URL ]] || die "The Hermes API URL is required."
if [[ -z ${HERMES_API_KEY:-} ]]; then
    read -r -s -p "  Hermes API_SERVER_KEY (hidden): " HERMES_API_KEY </dev/tty || true; echo
fi
ask WAKE_WORD "Wake word (alexa, computer... empty = keep)" ""

net="name=eth0,bridge=$BRIDGE,ip=dhcp${VLAN:+,tag=$VLAN}"
cat <<EOF

  ${bold}About to create:${reset}
    CT $CTID "$CT_HOSTNAME": Debian 13, unprivileged, $CORES cores, $RAM MB RAM, $DISK GB on $STORAGE
    network $net, start at boot
    then install Echo Hermes Bridge inside it (Echo $ECHO_HOST, Hermes $HERMES_URL, language $LANGUAGE)
EOF
ok=""
read -r -p "  Go ahead? [y/N] " ok </dev/tty || true
[[ $ok =~ ^[YyOo] ]] || die "Cancelled, nothing was changed."

step "Getting the Debian 13 template"
pveam update >/dev/null
TEMPLATE=$(pveam available --section system | awk '{print $2}' | grep -E '^debian-13-standard' | sort -V | tail -1)
[[ -n $TEMPLATE ]] || die "No Debian 13 template found (pveam available --section system)."
pveam list "$TEMPLATE_STORAGE" | grep -q "$TEMPLATE" || pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"

step "Creating CT $CTID"
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
    --hostname "$CT_HOSTNAME" --cores "$CORES" --memory "$RAM" --swap 512 \
    --rootfs "$STORAGE:$DISK" --net0 "$net" \
    --unprivileged 1 --features nesting=1 --onboot 1 --ostype debian \
    --description "Echo Hermes Bridge: https://github.com/Naod6473/echo-hermes-bridge"
pct start "$CTID"

step "Waiting for the network"
for _ in $(seq 1 30); do
    pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1 && break
    sleep 2
done
pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1 || die "CT $CTID has no internet access. Check $BRIDGE and DHCP."

step "Installing Echo Hermes Bridge inside CT $CTID"
envfile=$(mktemp); chmod 600 "$envfile"
for v in ECHO_HOST HERMES_URL HERMES_API_KEY LANGUAGE WAKE_WORD; do printf '%s=%q\n' "$v" "${!v:-}" >>"$envfile"; done
pct push "$CTID" "$envfile" /root/echo-install.env --perms 600
rm -f "$envfile"
pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq curl >/dev/null \
    && set -a && . /root/echo-install.env && set +a && rm -f /root/echo-install.env \
    && curl -fsSL '$INSTALLER_URL' | bash"

ip=$(pct exec "$CTID" -- hostname -I | awk '{print $1}')
cat <<EOF

${green}${bold}CT $CTID is ready${reset} at ${bold}${ip}${reset}.
  Give this IP a DHCP reservation in your router (or set a static IP in the CT's network settings).
  Logs:      pct exec $CTID -- journalctl -u hermes-echo -f
  Settings:  pct exec $CTID -- nano /opt/echo-hermes-bridge/.env
EOF
