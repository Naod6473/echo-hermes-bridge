# Flashing an Echo Dot 2 (RS03QR, "biscuit") from Windows 11

The official guide is [echo-dot-assist's Windows doc](https://github.com/Gamer92000/echo-dot-assist/blob/main/docs/WINDOWS.md). Follow it. This page only lists the snags we hit in November 2026 and the fixes that worked, so you don't lose an afternoon on them.

Flashing can brick the Echo. Keep it off Wi-Fi and out of the Alexa app until it is unlocked.

We used Git Bash for every command.

## 1. "no release build of this commit"

`main` usually has no prebuilt release. Check out the latest release tag before running setup:

```bash
git fetch --tags
git tag --sort=-creatordate | head -3
git checkout <latest tag, e.g. v2026.11.05.000019-beta>
```

## 2. `firebreak.exe` blocked: "os error 4551"

On Windows 11 with **Smart App Control** on, the `firebreak.exe` that `uv` builds is blocked. **Do not turn Smart App Control off**: it cannot be turned back on without reinstalling Windows.

Instead, install firebreak as a Python package and run the unlock step yourself:

```bash
py -m pip install firebreak==0.8.0
py -m firebreak unlock fireos6
```

Plug the Echo in by USB when the tool asks, as described in the official guide. When it reports success, run `setup.sh` again and skip the unlock step (answer `s` when it offers it).

## 3. `secrets/fork.conf: arithmetic syntax error`

That is a bug in setup's progress spinner (fixed upstream after the v2026.11.05 tag). Pipe the output through `cat`, so it is not a terminal and the spinner is skipped:

```bash
scripts/setup.sh biscuit --from build | cat
```

## 4. `AttributeError: module 'os' has no attribute 'fchmod'`

`otatool.py` needs **Python 3.13 or newer** on Windows. With Smart App Control on, the python.org installer may be blocked as well (error `0xc0e90002`). Install **Python 3.13 from the Microsoft Store** instead; it then answers to `python3`:

```bash
python3 --version     # 3.13.x
```

Run setup again.

## 5. Done

At the end the Echo joins your Wi-Fi and shows up with a name like `Echo Dot 2 xxxxxx`. Give it a fixed IP in your router, open `http://<echo-ip>:28931/`, and press the action button to log in. Then go back to the [main README](../README.md#2-prepare-hermes).
