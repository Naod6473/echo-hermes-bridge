# Contributing

Thanks for helping! This project is deliberately small: one script, no Home Assistant, no framework. Changes that keep it that way are the easiest to merge.

## Run the tests

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt ruff
.venv/bin/ruff check --select E9,F bridge.py cleanup.py tests/
.venv/bin/python tests/fake_echo_test.py
```

The test starts a fake Echo (a minimal ESPHome API server), a fake Hermes and stubbed speech models, so it needs no hardware and no downloads.

## Good first contributions

- Built-in voice instructions for more languages (`DEFAULTS` in `bridge.py`).
- Reports from other Echo models (Echo Dot 3 2018, Echo 2): what worked, what didn't.
- Flashing notes for Linux, like [docs/flashing-echo-dot-2-windows.md](docs/flashing-echo-dot-2-windows.md).
- Streaming the reply so the Echo starts talking sooner.

## Pull requests

- One topic per pull request.
- Say how you tested it, ideally on a real Echo.
- Never commit a `.env`, a key, or a wake-word model whose licence doesn't allow redistribution.
