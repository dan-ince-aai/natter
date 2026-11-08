# Contributing to Natter

Thanks for helping. A few notes:

- **Run it from source.** `./build.sh` builds the Swift helpers and `Natter.app`. `./run.sh` runs the engine from a
  terminal with logs in front of you (macOS then asks for audio permissions for your terminal app instead of Natter).
- **Keep it one engine file.** `app.py` is deliberately a single file; small, readable changes beat new layers.
- **Be careful with system audio.** Anything that changes the Mac's default devices must save what it changed and
  restore it on stop, on quit and at the next launch (see `_route_system_output` and `restore_sound`).
- **Never commit `.env` or `config.json`.** They hold your API key and personal settings; `.gitignore` covers them.
- **Describe how you tested.** For audio changes, say which output device (wired, built-in, Bluetooth) you tried.
