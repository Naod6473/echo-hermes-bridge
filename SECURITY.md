# Security

- The bridge serves the spoken replies over plain HTTP on port 8765 so the Echo can download them. Keep that port on your LAN; **do not expose it to the internet**.
- The Echo's ESPHome API has no encryption key by default. Anyone on your LAN can connect to it. Keep IoT devices on a trusted network or VLAN.
- Your `HERMES_API_KEY` lives in `.env`, which is git-ignored. Remove keys and IP addresses from logs before sharing them in an issue.
- Hermes may have tools that act on your systems. Anyone in earshot of the Echo can talk to it. Use a voice profile with only the tools you are happy to expose that way.

To report a vulnerability, open a [private security advisory](../../security/advisories/new) instead of a public issue.
