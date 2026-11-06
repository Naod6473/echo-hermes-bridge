# Changelog

## Unreleased

- One-line installer for Debian / Ubuntu (`install.sh`).
- Proxmox VE script that creates a ready-made LXC (`deploy/proxmox-lxc.sh`).
- Docker image (`ghcr.io/naod6473/echo-hermes-bridge`) and `docker-compose.yml`.
- Dependabot, code of conduct, Discussions links in the issue chooser.

## 0.1.0 (2026-11-06)

First public release.

- ESPHome voice-assistant client that replaces Home Assistant for an Echo running echo-dot-assist.
- Speech to text with faster-whisper, text to speech with Piper, replies served over HTTP.
- Conversations kept by Hermes through `X-Hermes-Session-Id`, reset after `IDLE_MINUTES`.
- Selects a wake word on the Echo at start (`WAKE_WORD`).
- English and French built-in voice instructions.
- systemd unit, fake-Echo end-to-end test, flashing notes for an Echo Dot 2 from Windows 11.
