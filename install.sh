#!/usr/bin/env bash
# Echo Hermes Bridge installer for Debian / Ubuntu (LXC, VM, Raspberry Pi OS...).
#
#   curl -fsSL https://raw.githubusercontent.com/Naod6473/echo-hermes-bridge/main/install.sh | sudo bash
#
# It installs the bridge in /opt/echo-hermes-bridge, downloads a Piper voice, writes .env,
# checks the link to Hermes and starts the hermes-echo systemd service.
# Run it again at any time to update: your .env is kept.
#
# Unattended install: set the answers as environment variables, e.g.
#   curl -fsSL .../install.sh | sudo ECHO_HOST=192.168.1.50 HERMES_URL=http://10.0.0.5:8642/v1 \
#        HERMES_API_KEY=... LANGUAGE=en WAKE_WORD=computer bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Naod6473/echo-hermes-bridge}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/echo-hermes-bridge}"
SERVICE=hermes-echo

bold=$'\e[1m'; dim=$'\e[2m'; green=$'\e[32m'; yellow=$'\e[33m'; red=$'\e[31m'; reset=$'\e[0m'
step() { printf '\n%s==>%s %s%s%s\n' "$green" "$reset" "$bold" "$*" "$reset"; }
warn() { printf '%s!%s %s\n' "$yellow" "$reset" "$*"; }
die()  { printf '%sx%s %s\n' "$red" "$reset" "$*" >&2; exit 1; }

# Questions are read from the terminal even when the script is piped into bash.
TTY=/dev/tty
ask() {        # ask VAR "question" "default"
    local var=$1 q=$2 def=${3:-} ans
    if [[ -n "${!var:-}" ]]; then return; fi
    if [[ ! -r $TTY ]]; then printf -v "$var" '%s' "$def"; return; fi
    read -r -p "  $q${def:+ ${dim}[$def]${reset}}: " ans <"$TTY" || true
    printf -v "$var" '%s' "${ans:-$def}"
}
ask_secret() { # ask_secret VAR "question"
    local var=$1 q=$2 ans
    if [[ -n "${!var:-}" || ! -r $TTY ]]; then return; fi
    read -r -s -p "  $q: " ans <"$TTY" || true; echo >&2
    printf -v "$var" '%s' "$ans"
}

cat <<EOF
${bold}
  ┌──────────────────────────────────────────┐
  │   Echo Hermes Bridge installer           │
  │   talk to Hermes through your old Echo   │
  └──────────────────────────────────────────┘${reset}
EOF

[[ $EUID -eq 0 ]] || die "Run me as root, e.g.: curl -fsSL <url> | sudo bash"
command -v apt-get >/dev/null || die "This installer needs Debian or Ubuntu (apt-get). See the README for a manual install."

step "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip ca-certificates >/dev/null
python3 - <<'PY' || die "Python 3.11 or newer is required (Debian 12+, Ubuntu 24.04+)."
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY

step "Getting the bridge into $INSTALL_DIR"
if [[ -d $INSTALL_DIR/.git ]]; then
    git -C "$INSTALL_DIR" pull --ff-only -q && echo "  updated to $(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
else
    git clone -q --depth 1 -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

step "Installing Python dependencies (a few minutes)"
[[ -x .venv/bin/python ]] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if [[ -f .env ]]; then
    step "Keeping your existing .env"
    LANGUAGE=$(sed -n 's/^LANGUAGE=//p' .env | head -1)
    PIPER_VOICE=$(sed -n 's/^PIPER_VOICE=//p' .env | head -1)
else
    step "A few questions"
    ask LANGUAGE "Language (en, fr...)" "en"
    ask ECHO_HOST "Your Echo's IP address" ""
    ask HERMES_URL "Hermes API URL" "http://127.0.0.1:8642/v1"
    ask_secret HERMES_API_KEY "Hermes API_SERVER_KEY (hidden)"
    ask WAKE_WORD "Wake word to select on the Echo (alexa, computer, hey_jarvis... empty = keep)" ""
    case "${LANGUAGE%%-*}" in
        fr) def_voice=fr_FR-siwis-medium ;;
        de) def_voice=de_DE-thorsten-medium ;;
        es) def_voice=es_ES-davefx-medium ;;
        it) def_voice=it_IT-paola-medium ;;
        *)  def_voice=en_US-lessac-medium ;;
    esac
    ask VOICE_NAME "Piper voice" "$def_voice"
    PIPER_VOICE="voices/${VOICE_NAME}.onnx"
    [[ -n $ECHO_HOST ]] || warn "No Echo IP given: set ECHO_HOST in $INSTALL_DIR/.env later."

    # Fill .env from .env.example without any shell escaping problems.
    ECHO_HOST=$ECHO_HOST HERMES_URL=$HERMES_URL HERMES_API_KEY=${HERMES_API_KEY:-} LANGUAGE=$LANGUAGE \
    PIPER_VOICE=$PIPER_VOICE WAKE_WORD=$WAKE_WORD python3 - <<'PY'
import os
keys = ["ECHO_HOST", "HERMES_URL", "HERMES_API_KEY", "LANGUAGE", "PIPER_VOICE", "WAKE_WORD"]
out = []
for line in open(".env.example", encoding="utf-8"):
    k = line.split("=", 1)[0]
    out.append(f"{k}={os.environ[k]}\n" if "=" in line and not line.startswith("#") and k in keys else line)
open(".env", "w", encoding="utf-8").writelines(out)
PY
    chmod 600 .env
fi

step "Downloading the voice ${PIPER_VOICE:-}"
mkdir -p voices
voice_name=$(basename "${PIPER_VOICE:-voices/en_US-lessac-medium.onnx}" .onnx)
if [[ -f voices/$voice_name.onnx ]]; then
    echo "  already there"
elif ! (cd voices && ../.venv/bin/python -m piper.download_voices "$voice_name") >/tmp/hermes-echo-voice.log 2>&1; then
    warn "Could not download $voice_name: $(tail -1 /tmp/hermes-echo-voice.log)"
    warn "Pick another one at https://rhasspy.github.io/piper-samples/ and set PIPER_VOICE in .env"
else
    echo "  done"
fi
rm -f /tmp/hermes-echo-voice.log

step "Checking the link to Hermes"
if .venv/bin/python bridge.py --ask "Say OK." >/tmp/hermes-echo-check.txt 2>&1; then
    printf '  Hermes answered: %s\n' "$(tail -1 /tmp/hermes-echo-check.txt)"
else
    warn "Hermes did not answer. Check HERMES_URL and HERMES_API_KEY in $INSTALL_DIR/.env"
    tail -3 /tmp/hermes-echo-check.txt | sed 's/^/    /'
fi
rm -f /tmp/hermes-echo-check.txt

if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
    step "Starting the $SERVICE service"
    sed "s#/opt/echo-hermes-bridge#$INSTALL_DIR#g" deploy/hermes-echo.service >/etc/systemd/system/$SERVICE.service
    systemctl daemon-reload
    systemctl enable -q $SERVICE
    systemctl restart $SERVICE
    sleep 2
    systemctl is-active -q $SERVICE && echo "  running" || warn "The service did not start: journalctl -u $SERVICE -n 50"
else
    warn "No systemd here: start the bridge with  cd $INSTALL_DIR && .venv/bin/python bridge.py"
fi

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
cat <<EOF

${green}${bold}Done!${reset}

  The first start downloads the Whisper model (about 500 MB), give it a minute.
  Follow it with:    ${bold}journalctl -u $SERVICE -f${reset}
  Settings:          ${bold}$INSTALL_DIR/.env${reset}  then  systemctl restart $SERVICE
  Update later:      run this installer again

  The Echo downloads replies from ${bold}${ip:-this machine}:8765${reset}: allow that port if a firewall is on.
  Then say the wake word and ask Hermes something.
EOF
