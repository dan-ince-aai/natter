"""Natter: real-time speech translation for any app on your Mac.

Two lanes, each a full pipeline:
  You -> them   your mic -> AssemblyAI streaming STT (+ LLM Gateway translation on the same socket)
                -> AssemblyAI TTS -> a virtual mic (BlackHole) that Zoom / Slack / Meet use as input
  Them -> you   everything the Mac plays (a system audio tap that leaves out this app's own audio),
                or any input device -> STT + LLM -> TTS -> your headphones

UI at http://127.0.0.1:18765. Run with ./run.sh, or open "Natter.app".
"""

import asyncio
import base64
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import deque
from pathlib import Path

import numpy as np
import sounddevice as sd
import websockets
from aiohttp import web
from scipy.signal import resample_poly

HERE = Path(__file__).resolve().parent
# Writable data (settings, API key, logs). Next to app.py when run from source; the packaged app points this at
# ~/Library/Application Support/Natter, because a signed .app bundle must not be written to.
DATA = Path(os.environ.get("NATTER_DATA") or HERE)
DATA.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA / "config.json"
SYSCAP = HERE / "bin" / "syscap"
AUDIODEV = HERE / "bin" / "audiodev"
VIRTUAL_MICS = ("blackhole", "zoomaudiodevice", "microsoft teams audio", "aggregate")
VIRTUAL_MIC = "BlackHole 2ch"
PORT = int(os.environ.get("LIVE_INTERPRETER_PORT", "18765"))
STT_URL = "wss://streaming.assemblyai.com/v3/ws"
TTS_URL = "wss://tts.us.assemblyai.com/v1/ws"
SYSTEM_INPUT = "System audio (everything you hear)"
MUTE_SUPPORTED = False  # see SystemSource.start
STT_RATE = 16000
SEGMENT_AFTER_S = 1.5  # end a turn at the first sentence end after this long...
SEGMENT_MAX_S = 4.5    # ...and in any case after this long, so continuous speech (videos) is translated as it goes
SENTENCE_END = re.compile(r"[.?!\u3002\uff1f\uff01\u2026][\"'\u201d\u00bb)]*\s*$")
TTS_RATES = (48000, 44100, 24000, 16000)

TTS_LANGUAGES = ["English", "Spanish", "French", "German", "Italian", "Portuguese"]
SOURCE_LANGUAGES = ["Any language", "English", "Spanish", "French", "German", "Italian", "Portuguese", "Dutch",
                    "Hindi", "Mandarin Chinese", "Japanese", "Korean", "Arabic", "Russian", "Turkish", "Polish",
                    "Vietnamese", "Ukrainian", "Swedish", "Indonesian", "Tagalog"]
VOICES = {  # from the TTS docs' starter list; any voice_id from the dashboard catalog also works
    "English": [("aai-kyla", "Kyla (US, young woman)"), ("aai-titus", "Titus (US, deep male)"),
                ("aai-peter", "Peter (US, soft male)"), ("aai-sara", "Sara (US, even woman)"),
                ("aai-nancy", "Nancy (British woman)"), ("aai-buford", "Buford (British male)"),
                ("aai-steven", "Steven (Australian male)"), ("aai-troy", "Troy (Scottish male)")],
    "Spanish": [("aai-antonioluis", "Antonio Luis (Mexican, deep male)"), ("aai-constanza", "Constanza (Mexican woman)"),
                ("aai-malvina", "Malvina (Mexican, soft woman)"), ("aai-oriol", "Oriol (Mexican male)")],
    "French": [("aai-donatien", "Donatien (male)")],
    "German": [("aai-magdalena", "Magdalena (woman)"), ("aai-gunther", "Günther (male)"), ("aai-jorg", "Jörg (male)")],
    "Italian": [("aai-giada", "Giada (woman)")],
    "Portuguese": [("aai-nelson", "Nelson (Brazilian male)"), ("aai-benicio", "Benício (male)")],
}
STT_MODELS = ["universal-3-6-pro", "universal-3-7-preview", "universal-3-5-pro", "universal-streaming-multilingual", "universal-streaming-english"]
# Models that answer through the streaming socket's llm_gateway (tested 2026-11-07). Others (gemma-4-31b,
# gpt-5.6-terra, gpt-6-astra, glm-5.3) work on LLM Gateway but return nothing on the socket, so the app calls
# LLM Gateway itself for them ("direct" route). Measured translation latency, direct: gemini-2.5-flash-lite ~1.0 s,
# claude-haiku-4-5 1.0 s, gemini-3.5-flash-lite 1.1 s, gpt-5.6-terra 1.3 s, gpt-5.6-luna 1.4 s, gemma-4-31b 1.4 s,
# glm-5.3 2.1 s, gpt-6-astra 2.2 s. On the socket, gemini-2.5-flash-lite lands ~0.7 s after the turn ends.
SOCKET_MODELS = {"gemini-2.5-flash-lite", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "claude-haiku-4-5-20251001",
                 "claude-haiku-5-5", "gemini-3.5-flash", "gpt-5.6-luna"}
LLM_MODELS = ["gemini-2.5-flash-lite", "claude-haiku-4-5-20251001", "gemini-3.5-flash-lite", "gpt-5.6-terra",
              "gpt-5.6-luna", "gemma-4-31b", "claude-haiku-5-5", "glm-5.3", "gpt-6-astra"]
GATEWAY_URL = "https://llm-gateway.assemblyai.com/v1/chat/completions"

PRESETS = {
    "Translate": (
        "You are a live interpreter on a call. Translate what the speaker just said from {source} into natural, "
        "spoken {target}. Keep the speaker's meaning, tone and register, and keep names, numbers and product names "
        "as they are. Reply with only the {target} translation, with no quotes, notes or explanations. If there is "
        "nothing to translate (only filler or noise), reply with nothing.\n\n"
        "The conversation so far, oldest first, for context only (do not translate it): {{history_5}}\n\n"
        "Translate this: {{turn}}"),
    "Clarify": (
        "Restate what the speaker just said in clear, simple {target}, in the first person as the speaker, keeping "
        "the exact meaning. Drop filler and false starts. Reply with only the restated words. If there is nothing "
        "meaningful, reply with nothing.\n\n"
        "The conversation so far, oldest first, for context only: {{history_5}}\n\n"
        "Speaker: {{turn}}"),
    "Explain": (
        "You are whispering to a listener on a call. In one short sentence of {target}, say what the speaker means, "
        "and explain any jargon or acronym in passing. Reply with only that sentence. If the speaker is still "
        "building up to their point and explaining now would be premature, reply with exactly WAIT; when you do "
        "explain, cover everything since your last explanation, using the conversation so far. If it was small "
        "talk or filler, reply with nothing.\n\n"
        "The conversation so far, oldest first, for context only: {{history_5}}\n\n"
        "Speaker: {{turn}}"),
}

DEFAULT_LANES = {
    "out": {"label": "You → them", "input": "", "output": "BlackHole 2ch", "monitor": "", "source": "English",
            "target": "Spanish", "voice": "aai-antonioluis", "preset": "Translate", "prompt": PRESETS["Translate"],
            "stt_model": "universal-3-6-pro", "llm_model": "gemini-2.5-flash-lite", "mode": "min_latency",
            "auto_segment": True, "volume": 1.0, "auto_mic": True,
            "diarize": False, "max_speakers": 2},
    "in": {"label": "Them → you", "input": SYSTEM_INPUT, "output": "", "monitor": "", "source": "Any language",
           "target": "English", "voice": "aai-kyla", "preset": "Translate", "prompt": PRESETS["Translate"],
           "stt_model": "universal-3-6-pro", "llm_model": "gemini-2.5-flash-lite", "mode": "min_latency",
           "auto_segment": True, "volume": 1.0, "mute_original": False,
           "diarize": True, "max_speakers": 4},
}


VERSION = "0.1.1"
# Every request to AssemblyAI says it comes from Natter (and which version), so usage can be told apart from other
# clients. No personal or device data is in it.
USER_AGENT = f"Natter/{VERSION} (macOS; +https://github.com/dan-ince-aai/natter)"


def ws_connect(url, headers, **kw):
    """websockets >= 14 takes additional_headers; older versions (legacy client) take extra_headers."""
    kw.setdefault("user_agent_header", USER_AGENT)
    try:
        from websockets.asyncio.client import connect
        return connect(url, additional_headers=headers, **kw)
    except ImportError:
        return websockets.connect(url, extra_headers=headers, **kw)


KEYCHAIN = ("dev.natter.app", "assemblyai")  # service, account: the key lives in the login Keychain, like Blurt's


def _keychain_get():
    try:
        r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN[0], "-a", KEYCHAIN[1], "-w"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def api_key():
    """The AssemblyAI key: environment, then the macOS Keychain, then a .env file (running from source). '' if none
    yet: the window then asks for one."""
    if os.environ.get("ASSEMBLYAI_API_KEY"):
        return os.environ["ASSEMBLYAI_API_KEY"].strip()
    global _key_cache
    if _key_cache:
        return _key_cache
    key = _keychain_get()
    if not key:
        for env in (DATA / ".env", HERE / ".env"):
            if env.exists():
                for line in env.read_text().splitlines():
                    if line.startswith("ASSEMBLYAI_API_KEY="):
                        key = line.split("=", 1)[1].strip().strip('"')
                        break
            if key:
                break
    _key_cache = key
    return key


_key_cache = ""


def save_api_key(key):
    """Store the key in the login Keychain (-U updates an existing item). Never written to disk in plain text."""
    global _key_cache
    r = subprocess.run(["/usr/bin/security", "add-generic-password", "-U", "-s", KEYCHAIN[0], "-a", KEYCHAIN[1],
                        "-l", "Natter: AssemblyAI API key", "-w", key.strip()], capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        raise RuntimeError("couldn't save the key to your Keychain")
    _key_cache = key.strip()


def load_config():
    cfg = {"lanes": json.loads(json.dumps(DEFAULT_LANES))}
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text())
            for k, v in saved.get("lanes", {}).items():
                if k in cfg["lanes"]:
                    cfg["lanes"][k].update(v)
        except Exception as e:  # noqa: BLE001
            print("config.json unreadable, using defaults:", e)
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def render_prompt(lane_cfg):
    p = lane_cfg["prompt"]
    src = lane_cfg["source"]
    src = "whatever language is spoken" if src == "Any language" else src
    p = p.replace("{source}", src).replace("{target}", lane_cfg["target"])
    if "{{turn}}" not in p:
        p += "\n\n{{turn}}"
    style = (lane_cfg.get("style_note") or "").strip()
    if style:  # how you want to come across: a register, a vibe, or a brand's voice. It wins over the speaker's register.
        rule = ("How to say it (this overrides the speaker's own tone and register where they differ, but never "
                f"change the meaning or invent facts): {style}")
        p = p.replace("\n\nThe conversation so far", f"\n\n{rule}\n\nThe conversation so far", 1) if "\n\nThe conversation so far" in p \
            else p.replace("{{turn}}", f"{rule}\n\n{{{{turn}}}}", 1)
    return p


# ---------------------------------------------------------------- devices
def list_devices():
    out = []
    default_in, default_out = sd.default.device
    for i, d in enumerate(sd.query_devices()):
        out.append({"index": i, "name": d["name"], "inputs": d["max_input_channels"], "outputs": d["max_output_channels"],
                    "rate": int(d["default_samplerate"]), "default_input": i == default_in, "default_output": i == default_out})
    return out


def find_device(name, kind):
    """Index of the device called `name` (exact, then substring), or the system default for '' ."""
    devs = list_devices()
    key = "inputs" if kind == "input" else "outputs"
    if not name:
        for d in devs:
            if d["default_" + kind] and d[key] > 0:
                return d
        raise RuntimeError(f"no default {kind} device")
    for d in devs:
        if d["name"] == name and d[key] > 0:
            return d
    for d in devs:
        if name.lower() in d["name"].lower() and d[key] > 0:
            return d
    raise RuntimeError(f"{kind} device not found: {name}")


def default_input_name():
    try:
        return subprocess.run([str(AUDIODEV), "get-input"], capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def set_default_input(name):
    r = subprocess.run([str(AUDIODEV), "set-input", name], capture_output=True, text=True, timeout=3)
    return r.returncode == 0


def default_output_name():
    try:
        return subprocess.run([str(AUDIODEV), "get-output"], capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def set_default_output(name):
    r = subprocess.run([str(AUDIODEV), "set-output", name], capture_output=True, text=True, timeout=3)
    return r.returncode == 0


# "Mute original" routes the Mac's sound output into a silent virtual device and listens to it there; the translation
# plays straight to your real headphones. Unmuting is just setting the output back (no process taps involved). It
# needs its own virtual device, separate from the one Talk uses as your virtual mic, or call audio leaks into it.
MUTE_DEVICES = ("BlackHole 16ch", "BlackHole 64ch")


def mute_device():
    names = {d["name"] for d in list_devices() if d["inputs"] > 0 and d["outputs"] > 0}
    return next((n for n in MUTE_DEVICES if n in names), "")


def real_output_name(preferred=""):
    """Your real headphones/speakers: the chosen output, else the default unless it's virtual (as it is while
    muting), else the one saved before muting, else the first real output."""
    if preferred:
        return preferred
    current = default_output_name()
    if current and not is_virtual(current):
        return current
    saved = hub.cfg.get("restore_output", "") if "hub" in globals() else ""
    outs = [d["name"] for d in list_devices() if d["outputs"] > 0 and not is_virtual(d["name"])]
    for pick in [saved] + [n for n in outs if "airpods" in n.lower()] + outs:
        if pick and pick in outs:
            return pick
    return ""


def restore_sound():
    """Put the Mac's default output and input back on real devices (the escape hatch, also run at launch)."""
    done = []
    out_saved = hub.cfg.pop("restore_output", None)
    if is_virtual(default_output_name()):
        target = out_saved or real_output_name()
        if target and set_default_output(target):
            done.append(f"output: {target}")
    in_saved = hub.cfg.pop("restore_input", None)
    if is_virtual(default_input_name()):
        target = in_saved or real_mic_name()
        if target and set_default_input(target):
            done.append(f"mic: {target}")
    save_config(hub.cfg)
    return done


def is_virtual(name):
    return any(v in (name or "").lower() for v in VIRTUAL_MICS)


def real_mic_name(preferred=""):
    """The mic to listen to: the chosen one, else the system default unless that's a virtual device (which it is
    while the app routes your translated voice there), else AirPods, else the built-in mic."""
    if preferred:
        return preferred
    names = [d["name"] for d in list_devices() if d["inputs"] > 0 and not is_virtual(d["name"])]
    current = default_input_name()
    if current and not is_virtual(current):
        return current
    saved = hub.cfg.get("restore_input", "") if "hub" in globals() else ""
    for pick in [saved] + [n for n in names if "airpods" in n.lower()] + [n for n in names if "macbook" in n.lower()] + names:
        if pick and pick in names:
            return pick
    return ""


def to_rate(x, sr_in, sr_out):  # whole clips only; streamed chunks go through Resampler
    if sr_in == sr_out or len(x) == 0:
        return x
    g = np.gcd(int(sr_in), int(sr_out))
    return resample_poly(x, sr_out // g, sr_in // g).astype(np.float32)


# ---------------------------------------------------------------- playback
class Player:
    """A sounddevice output stream fed from a queue of float32 mono chunks.

    Gapless by design: chunks are queued untouched (no per-chunk processing, which clicks at every edge), playback
    starts only once PREBUFFER seconds are queued so network jitter can't starve it mid-word, and if it does run dry
    it fades out and re-buffers instead of stuttering. end_slot() lets the tail of a sentence play without waiting."""

    PREBUFFER = 0.15

    def __init__(self, device_name, volume=1.0):
        d = find_device(device_name, "output")
        self.name = d["name"]
        self.rate = d["rate"]
        self.channels = min(2, d["outputs"])
        self.volume = volume
        self.buf = deque()
        self.cur = np.zeros(0, np.float32)
        self.lock = threading.Lock()
        self._init_state()
        self.stream = sd.OutputStream(device=d["index"], samplerate=self.rate, channels=self.channels, dtype="float32",
                                      blocksize=1024, latency="high", callback=self._cb)
        # 1024-frame blocks with the high-latency buffer: the callback runs in Python, so a busy moment elsewhere in
        # the app (decoding a message, the capture stream) must not starve the speakers. "low" crackled on AirPods.
        print(f"player: {self.name} {self.rate} Hz, {self.channels} ch, output latency {self.stream.latency * 1000:.0f} ms", flush=True)
        self.stream.start()

    def _init_state(self):
        self.queued = 0          # samples waiting (buf + cur)
        self.primed = False      # playing, as opposed to waiting for the prebuffer
        self.ending = False      # the current sentence has all arrived: play its tail even if short
        self.underflows = 0
        self.fade_in = False     # ease back in after running dry
        self.bed = deque()       # the original audio, played quietly under the translation while muting
        self.bed_queued = 0
        self.bed_gain = 0.0
        self.rs = None           # stateful resampler, only when the source rate differs from the device's

    def _cb(self, outdata, frames, _t, status):
        if status and getattr(status, "output_underflow", False):
            self.underflows += 1
        out = np.zeros(frames, np.float32)
        n = 0
        with self.lock:
            if not self.primed and (self.queued >= self.PREBUFFER * self.rate or (self.ending and self.queued > 0)):
                self.primed = True
            if self.primed:
                while n < frames:
                    if len(self.cur) == 0:
                        if not self.buf:
                            break
                        self.cur = self.buf.popleft()
                    k = min(frames - n, len(self.cur))
                    out[n:n + k] = self.cur[:k]
                    self.cur = self.cur[k:]
                    n += k
                self.queued -= n
                if self.fade_in and n:
                    f = min(n, int(self.rate * 0.008))
                    out[:f] *= np.linspace(0, 1, f, dtype=np.float32)
                    self.fade_in = False
                if n < frames:  # ran dry
                    if n:  # fade the last few ms instead of cutting to silence (a cut is a click)
                        f = min(n, int(self.rate * 0.008))
                        out[n - f:n] *= np.linspace(1, 0, f, dtype=np.float32)
                    self.fade_in = not self.ending  # mid-sentence: the resume should fade in too
                    self.primed = False
                    self.ending = False
            if self.bed_queued:  # mix the original underneath, at bed_gain
                m = 0
                while m < frames and self.bed:
                    b = self.bed[0]
                    k = min(frames - m, len(b))
                    out[m:m + k] += b[:k] * self.bed_gain
                    if k == len(b):
                        self.bed.popleft()
                    else:
                        self.bed[0] = b[k:]
                    m += k
                self.bed_queued -= m
        outdata[:] = (out * self.volume)[:, None]

    GAIN = 2.5  # TTS peaks around -9 dBFS; +8 dB puts it level with music and video, with a soft limiter above 0.9

    def push(self, mono, rate):
        """Queue audio (gain + limiter only, both per-sample, so chunk edges stay seamless); returns its length in s."""
        x = np.asarray(mono, np.float32) * self.GAIN
        a = np.abs(x)
        if a.max(initial=0) > 0.9:
            x = np.where(a > 0.9, np.sign(x) * (0.9 + 0.1 * np.tanh((a - 0.9) / 0.1)), x).astype(np.float32)
        if rate != self.rate:
            if self.rs is None or self.rs.sr_in != rate:
                self.rs = Resampler(rate, self.rate)
            x = self.rs(x)
        if not len(x):
            return 0.0
        with self.lock:
            self.buf.append(x)
            self.queued += len(x)
            self.ending = False
        return len(x) / self.rate

    def push_bed(self, mono, rate, gain):
        """Queue original audio to play under the translation. Kept to 150 ms so it stays in sync with the video."""
        if rate != self.rate or gain <= 0:
            return
        with self.lock:
            self.bed_gain = gain
            self.bed.append(np.asarray(mono, np.float32))
            self.bed_queued += len(mono)
            while self.bed_queued > self.rate * 0.15 and len(self.bed) > 1:
                self.bed_queued -= len(self.bed.popleft())

    def end_slot(self):
        with self.lock:
            self.ending = True

    def clear(self):
        with self.lock:
            self.buf.clear()
            self.cur = np.zeros(0, np.float32)
            self.queued = 0
            self.primed = False

    @property
    def queued_seconds(self):
        return self.queued / self.rate

    @property
    def busy(self):
        return self.queued > 0

    def close(self):
        if self.underflows:
            print(f"player: {self.name} had {self.underflows} output underflows", flush=True)
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:  # noqa: BLE001
            pass


class Resampler:
    """Linear-interpolation resampler that carries its phase across chunks, so chunk edges join seamlessly."""

    def __init__(self, sr_in, sr_out):
        self.sr_in, self.step, self.phase, self.last = sr_in, sr_in / sr_out, 0.0, None

    def __call__(self, x):
        if self.last is not None:
            x = np.concatenate([[self.last], x])
        if len(x) < 2:
            self.last = x[-1] if len(x) else self.last
            return np.zeros(0, np.float32)
        pos = np.arange(self.phase, len(x) - 1, self.step)
        i = pos.astype(np.int64)
        y = x[i] + (x[i + 1] - x[i]) * (pos - i)
        self.phase = (pos[-1] + self.step) - (len(x) - 1) if len(pos) else self.phase - (len(x) - 1)
        self.last = x[-1]
        return y.astype(np.float32)


def stretch(x, rate, speed):
    """Speed speech up without changing its pitch (WSOLA), after shortening long pauses."""
    x = np.asarray(x, np.float32)
    # long pauses inside the sentence -> 150 ms
    win = int(rate * 0.02)
    if len(x) > win * 4:
        env = np.sqrt(np.convolve(x * x, np.ones(win) / win, "same"))
        quiet = env < 0.008
        keep = np.ones(len(x), bool)
        i, n, maxq = 0, len(x), int(rate * 0.15)
        while i < n:
            if quiet[i]:
                j = i
                while j < n and quiet[j]:
                    j += 1
                if j - i > maxq:
                    keep[i + maxq // 2: j - maxq // 2] = False
                i = j
            else:
                i += 1
        x = x[keep]
    if speed <= 1.02 or len(x) < rate * 0.1:
        return x
    N = int(rate * 0.03)              # 30 ms frames
    hs = N // 2                       # output hop
    ha = hs * speed                   # analysis hop
    tol = int(rate * 0.008)           # search +-8 ms for the best-matching frame
    w = np.hanning(N).astype(np.float32)
    nframes = int((len(x) - N - tol) / ha)
    if nframes < 2:
        return x
    out = np.zeros(nframes * hs + N, np.float32)
    norm = np.zeros_like(out)
    d = 4  # decimate the similarity search; it only has to find the phase, not be exact
    prev = 0
    for k in range(nframes):
        nominal = int(k * ha)
        if k == 0:
            pos = nominal
        else:
            target = x[prev + hs: prev + hs + N: d]                     # what would naturally follow the last frame
            lo, hi = max(0, nominal - tol), min(len(x) - N, nominal + tol)
            seg = x[lo: hi + N: d]
            if len(target) and len(seg) >= len(target):
                c = np.correlate(seg, target, "valid")
                pos = lo + int(np.argmax(c)) * d
            else:
                pos = nominal
        out[k * hs: k * hs + N] += x[pos: pos + N] * w
        norm[k * hs: k * hs + N] += w
        prev = pos
    norm[norm < 1e-3] = 1.0
    return out / norm


# ---------------------------------------------------------------- TTS
class TTS:
    """One AssemblyAI TTS WebSocket session per lane, reopened when it drops."""

    def __init__(self, lane, voice, language, rate):
        self.lane, self.voice, self.language, self.rate = lane, voice, language.lower(), rate
        self.ws = None
        self.reader = None
        self.keepalive = None
        self.pending = {}  # flush_id -> (t_sent, turn_order, playback slot)
        self.sent = 0
        self.lock = asyncio.Lock()

    async def connect(self):
        q = urllib.parse.urlencode({"voice_id": self.voice, "language": self.language, "sample_rate": self.rate,
                                    "encoding": "pcm_s16le", "word_boundaries": "false"})
        self.ws = await ws_connect(f"{TTS_URL}?{q}", {"Authorization": api_key()}, max_size=None, ping_interval=20)
        first = json.loads(await self.ws.recv())
        if first.get("type") == "Error":
            raise RuntimeError(f"TTS {first.get('error_code')}: {first.get('error')}")
        self.rate = first["configuration"]["sample_rate"]
        self.sent = 0
        self.pending.clear()
        self.reader = asyncio.create_task(self._read())
        self.keepalive = asyncio.create_task(self._keepalive())

    async def _keepalive(self):
        while True:
            await asyncio.sleep(60)
            try:
                await self.ws.send(json.dumps({"type": "KeepAlive"}))
            except Exception:  # noqa: BLE001
                return

    async def _read(self):
        """Hand each request's audio to the lane's playback queue, tagged with its slot."""
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                t = m.get("type")
                if t == "Audio":
                    entry = self.pending.get(m.get("flush_id"))
                    if entry:
                        pcm = np.frombuffer(base64.b64decode(m["audio"]), "<i2").astype(np.float32) / 32768
                        self.lane.tts_audio(entry[2], pcm, self.rate)
                elif t == "FlushDone":
                    entry = self.pending.pop(m.get("flush_id"), None)
                    if entry:
                        self.lane.tts_done(entry[2])
                elif t == "Error":
                    self.lane.emit("error", message=f"TTS {m.get('error_code')}: {m.get('error')}")
        except websockets.ConnectionClosed:
            pass
        except Exception as e:  # noqa: BLE001
            self.lane.emit("error", message=f"TTS reader: {e}")
        finally:  # a dropped session must not hold up the playback queue
            for _, _, slot in self.pending.values():
                self.lane.tts_done(slot)
            self.pending.clear()

    async def speak(self, text, turn, slot):
        async with self.lock:
            for attempt in (1, 2):
                try:
                    if self.ws is None or self.ws.close_code is not None:
                        await self.connect()
                    await self.ws.send(json.dumps({"type": "Generate", "text": text}))
                    await self.ws.send(json.dumps({"type": "Flush"}))
                    self.pending[self.sent] = (time.monotonic(), turn, slot)
                    self.lane.slot_sent(slot)
                    self.sent += 1
                    return
                except Exception as e:  # noqa: BLE001
                    self.ws = None
                    if attempt == 2:
                        self.lane.emit("error", message=f"TTS: {e}")
                        self.lane.tts_done(slot)

    async def cancel(self):
        if self.ws is not None and self.ws.close_code is None:
            try:
                await self.ws.send(json.dumps({"type": "Cancel"}))
            except Exception:  # noqa: BLE001
                pass

    async def close(self):
        for task in (self.keepalive, self.reader):
            if task:
                task.cancel()
        if self.ws is not None:
            try:
                await self.ws.send(json.dumps({"type": "Terminate"}))
                await asyncio.wait_for(self.ws.close(), 2)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- capture
class MicSource:
    def __init__(self, device_name, on_audio):
        d = find_device(device_name, "input")
        self.name, self.rate = d["name"], d["rate"]
        self.on_audio = on_audio
        self.stream = sd.InputStream(device=d["index"], samplerate=self.rate, channels=1, dtype="float32",
                                     blocksize=int(self.rate * 0.05), callback=self._cb)

    def _cb(self, indata, _frames, _t, _status):
        self.on_audio(indata[:, 0].copy(), self.rate)

    def start(self):
        self.stream.start()

    def stop(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:  # noqa: BLE001
            pass


class RouteSource:
    """Listens to the silent virtual device that the Mac's sound output is routed into while muting."""

    name = SYSTEM_INPUT

    def __init__(self, device_name, on_audio, on_bed=None):
        d = find_device(device_name, "input")
        self.rate, self.on_audio, self.on_bed = d["rate"], on_audio, on_bed
        self.stream = sd.InputStream(device=d["index"], samplerate=self.rate, channels=min(2, d["inputs"]), dtype="float32",
                                     blocksize=int(self.rate * 0.05), callback=self._cb)

    def _cb(self, indata, _frames, _t, _status):
        mono = indata.mean(axis=1).astype(np.float32)
        self.on_audio(mono, self.rate)
        if self.on_bed:
            self.on_bed(mono, self.rate)

    def start(self):
        self.stream.start()

    def stop(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:  # noqa: BLE001
            pass


class SystemSource:
    """bin/syscap: a Core Audio tap of everything the Mac plays, leaving out this process's own audio."""

    name = SYSTEM_INPUT

    def __init__(self, on_audio, on_error, mute=False):
        self.on_audio, self.on_error, self.mute = on_audio, on_error, mute
        self.proc = None
        self.rate = 48000

    def start(self):
        if not SYSCAP.exists():
            raise RuntimeError("bin/syscap is missing: run ./build.sh")
        # "Mute original" (the tap's mutedWhenTapped) is switched off: it's the prime suspect for a coreaudiod
        # wedge on AirPods (2026-11-08) and isn't verified safe. Re-enable only after testing on Bluetooth output.
        args = [str(SYSCAP), "--exclude-pid", str(os.getpid())] + (["--mute"] if self.mute and MUTE_SUPPORTED else [])
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0)
        while True:  # header lines (CLOCK ..., RATE ...) come first on stderr
            line = self.proc.stderr.readline().decode().strip()
            print(f"syscap: {line}", flush=True)
            if line.startswith("RATE"):
                self.rate = int(line.split()[1])
                break
            if not line or line.startswith("ERROR"):
                self.stop()
                raise RuntimeError(f"system audio capture failed: {line or 'helper exited'}")
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self):
        chunk = int(self.rate * 0.05) * 4
        while self.proc and self.proc.poll() is None:
            data = self.proc.stdout.read(chunk)
            if not data:
                break
            self.on_audio(np.frombuffer(data[: len(data) // 4 * 4], "<f4").copy(), self.rate)

    def _read_err(self):
        for line in self.proc.stderr:
            s = line.decode().strip()
            print(f"syscap: {s}", flush=True)
            if s.startswith(("ERROR", "WARN")):
                self.on_error(s)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


# ---------------------------------------------------------------- a lane
class Lane:
    def __init__(self, key, hub):
        self.key, self.hub = key, hub
        self.cfg = None
        self.running = False
        self.task = None
        self.loop = None
        self.audio_q = None
        self.source = None
        self.players = []
        self.tts = None
        self.final_at = {}  # turn -> monotonic time of the final transcript
        # Each STT session numbers its turns from 0. A language switch or a reconnect opens a new session, so turns
        # are offset to stay unique and increasing for the whole life of the lane (the transcript and the playback
        # queue are keyed by them; a repeat would overwrite or reorder earlier lines).
        self.turn_base, self.max_turn = 0, -1
        self.level = 0.0
        self.stats = {"turns": 0, "llm_ms": [], "tts_ms": []}
        self.history = []  # final transcripts, for {{history_N}} on the direct route
        self.http = None
        self.switched_mic = None  # the real default mic we replaced with the virtual one
        self.ws = None
        self.sent_ms = 0.0            # audio sent on this session, ms
        self.anchor = (0.0, 0.0)      # (audio ms, monotonic time) at the last send, to place word timestamps in time
        self.turn_started = {}
        self.forced = set()
        self.speech_at = None         # when the current turn's speech started (SpeechStarted), None between turns
        self.split_turns = set()      # turns translated by the app itself (split by speaker, or trimmed), so the
                                      # socket's own translation of them is ignored
        self.last_words = []          # normalised last words of the previous final, to drop repeats after a cut
        self.timing = {}              # turn -> {speech_end, detect_ms, llm_ms}
        self.turn_speaker = {}        # turn -> speaker label
        self.speaker_voice = {}       # speaker label -> voice_id
        self.voices = {}              # voice_id -> TTS session
        self.slots = {}               # playback slot -> {turn, t_sent, chunks, done, started, played, trimmed}
        self.slot_order = deque()     # slots in the order the speech was heard (reserved at the final transcript)
        self.turn_slot = {}           # turn -> its reserved slot, waiting for the translation
        self.next_slot = 0
        self.t_start = time.monotonic()

    # called from audio threads
    def on_audio(self, mono, rate):
        if not self.running:
            return
        self.level = max(self.level * 0.6, float(np.sqrt(np.mean(mono ** 2))) if len(mono) else 0.0)
        pcm = (np.clip(to_rate(mono, rate, STT_RATE), -1, 1) * 32767).astype("<i2").tobytes()
        self.loop.call_soon_threadsafe(self.audio_q.put_nowait, pcm)

    def play(self, pcm, rate):
        durations = [p.push(pcm, rate) for p in self.players]
        return durations[0] if durations else 0.0

    def emit(self, kind, **kw):
        self.hub.broadcast({"lane": self.key, "type": kind, **kw})

    async def start(self, cfg):
        if self.running:
            await self.stop()
        self.cfg = dict(cfg)
        self.loop = asyncio.get_running_loop()
        self.audio_q = asyncio.Queue()
        self.stats = {"turns": 0, "llm_ms": [], "tts_ms": []}
        self.t_start = time.monotonic()
        self.timing.clear()
        try:
            muting = (self.cfg["input"] == SYSTEM_INPUT and self.cfg.get("mute_original")) and mute_device()
            if self.cfg["input"] == SYSTEM_INPUT and self.cfg.get("mute_original") and not muting:
                self.emit("notice", message="To mute the original, install BlackHole 16ch once: brew install blackhole-16ch")
            out_name = real_output_name(self.cfg["output"]) if muting else self.cfg["output"]
            self.players = [Player(out_name, self.cfg.get("volume", 1.0))]
            if self.cfg.get("monitor"):
                self.players.append(Player(self.cfg["monitor"], self.cfg.get("volume", 1.0)))
            rate = self.players[0].rate if self.players[0].rate in TTS_RATES else 48000
            self.tts_rate = rate
            self.speaker_voice.clear()
            self.voices = {}
            self.tts = await self._voice_session(self.cfg["voice"])  # open before the first reply so it's warm
            if muting:
                level = float(self.cfg.get("original_level", 0.1))  # 0 = silent, 0.1 = 10% under the translation
                self.source = RouteSource(muting, self.on_audio,
                                          on_bed=(lambda x, r: [p.push_bed(x, r, level) for p in self.players]) if level > 0 else None)
                self._route_system_output(muting)
            elif self.cfg["input"] == SYSTEM_INPUT:
                self.source = SystemSource(self.on_audio, lambda s: self.emit("error", message=s), mute=False)
            else:
                self.source = MicSource(real_mic_name(self.cfg["input"]), self.on_audio)
                if self.cfg.get("auto_mic") and is_virtual(self.cfg["output"]):
                    self._route_system_mic()
        except Exception as e:  # noqa: BLE001
            await self._teardown()
            self.emit("status", running=False, error=str(e))
            raise
        self.running = True
        self.task = asyncio.create_task(self._run())
        self.emit("status", running=True, input=self.source.name, output=[p.name for p in self.players])

    async def _run(self):
        """Keep a streaming session open while the lane runs; reconnect if it drops."""
        while self.running:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.emit("error", message=f"STT: {e}")
                await asyncio.sleep(1.5)

    async def _session(self):
        c = self.cfg
        # mode presets the turn detection: min_latency ends a complete-sounding turn after 128 ms of silence and
        # forces it at 640 ms, so speech is cut into short segments that are translated as soon as each ends.
        params = {"sample_rate": STT_RATE, "encoding": "pcm_s16le", "speech_model": c["stt_model"],
                  "format_turns": "true", "mode": c.get("mode") or "min_latency"}
        if c.get("diarize"):  # speaker labels on every turn, so each speaker gets their own voice
            params["speaker_labels"] = "true"
            params["max_speakers"] = int(c.get("max_speakers") or 4)
        if self.socket_route:  # translation runs server-side on the same socket
            params["llm_gateway"] = json.dumps({"model": c["llm_model"], "prompt": render_prompt(c), "max_tokens": 400})
        url = f"{STT_URL}?{urllib.parse.urlencode(params)}"
        async with ws_connect(url, {"Authorization": api_key()}, max_size=None, ping_interval=20) as ws:
            if not getattr(self.source, "_started", False):  # start capturing once the first session is up
                await asyncio.to_thread(self.source.start)
                self.source._started = True
            while not self.audio_q.empty():  # drop audio queued while reconnecting
                self.audio_q.get_nowait()
            self.ws, self.sent_ms, self.anchor = ws, 0.0, (0.0, time.monotonic())
            self.turn_base = self.max_turn + 1  # this session's turn 0 follows the last one heard
            self.turn_started.clear()
            self.forced.clear()
            self.speech_at = None
            sender = asyncio.create_task(self._send(ws))
            meter = asyncio.create_task(self._meter())
            try:
                async for raw in ws:
                    await self._on_message(json.loads(raw))
            finally:
                sender.cancel()
                meter.cancel()
            if ws.close_code not in (1000, None) and self.running:
                raise RuntimeError(f"closed {ws.close_code} {ws.close_reason}")

    async def _send(self, ws):
        buf = b""
        while True:
            buf += await self.audio_q.get()
            if len(buf) >= 1600:  # 50 ms at 16 kHz
                await ws.send(buf)
                self.sent_ms += len(buf) / 32  # 16 kHz, 2 bytes a sample
                self.anchor = (self.sent_ms, time.monotonic())
                buf = b""

    async def _meter(self):
        while True:
            await asyncio.sleep(0.12)
            self._pump()  # also unblocks a slot whose TTS request never answered
            if (self.speech_at is not None and self.cfg.get("auto_segment", True) and self.ws is not None
                    and time.monotonic() - self.speech_at > SEGMENT_MAX_S):  # partials are too sparse to rely on
                self.speech_at = None
                await self._force_endpoint()
            for p in self.players:
                if p.underflows != getattr(p, "_logged_uf", 0):
                    print(f"player: {p.name} output underflows so far: {p.underflows}", flush=True)
                    p._logged_uf = p.underflows
            self.emit("level", value=round(min(1.0, self.level * 6), 3),
                      speaking=any(p.busy for p in self.players))

    async def _on_message(self, m):
        t = m.get("type")
        if isinstance(m.get("turn_order"), int):
            m["turn_order"] += self.turn_base
            self.max_turn = max(self.max_turn, m["turn_order"])
        if t == "Begin":
            model = (m.get("configuration") or {}).get("model") or self.cfg["stt_model"]
            note = "" if model == self.cfg["stt_model"] else f" (asked for {self.cfg['stt_model']})"
            self.emit("session", id=m.get("id"), model=model + note)
        elif t == "SpeechStarted":
            self.speech_at = time.monotonic()
        elif t == "Turn":
            turn = m.get("turn_order")
            if self.speech_at is None:
                self.speech_at = time.monotonic()
            if m.get("end_of_turn"):
                self.speech_at = None
                if self._drop_repeat(turn, m):
                    return
                if self._split_by_speaker(turn, m):
                    return
                if self.cfg["stt_model"].startswith("universal-streaming") and not m.get("turn_is_formatted"):
                    return
                now = time.monotonic()
                self.final_at[turn] = now
                self.stats["turns"] += 1
                words = m.get("words") or []
                if words and words[-1].get("end") is not None:  # when the speaker actually stopped, in wall time
                    wall = lambda ms: self.anchor[1] - (self.anchor[0] - ms) / 1000  # stream ms -> monotonic time
                    speech_end = wall(words[-1]["end"])
                    self.timing[turn] = {"speech_start": wall(words[0].get("start") or words[-1]["end"]),
                                         "speech_end": speech_end, "detect_ms": max(0, int((now - speech_end) * 1000))}
                speaker = m.get("speaker_label") if self.cfg.get("diarize") else None
                speaker = speaker if speaker and speaker.isalpha() and speaker != "UNKNOWN" else None
                self.turn_speaker[turn] = speaker
                self.emit("final", turn=turn, text=m.get("transcript", ""), speaker=speaker)
                if m.get("transcript", "").strip():
                    # Hold this turn's place in line now, in the order it was spoken. A slow translation then
                    # delays what comes after it instead of being overtaken by it.
                    self.turn_slot[turn] = self.reserve_slot(turn)
                if m.get("_direct"):
                    asyncio.create_task(self._direct_llm(turn, m["transcript"]))
                elif self.socket_route:
                    asyncio.get_running_loop().call_later(6, self._check_translated, turn)
                elif m.get("transcript", "").strip():
                    asyncio.create_task(self._direct_llm(turn, m["transcript"]))
            else:
                text = m.get("transcript", "")
                self.emit("partial", turn=turn, text=text)
                self._maybe_segment(turn, text, m.get("words") or [])
        elif t == "LLMGatewayResponse":
            turn = m.get("turn_order")
            if turn in self.split_turns:  # already translated per speaker
                self.split_turns.discard(turn)
                return
            try:
                text = (m["data"]["choices"][0]["message"]["content"] or "").strip()
            except (KeyError, IndexError, TypeError):
                text = ""
                self.emit("error", message=f"LLM: {json.dumps(m.get('data'))[:300]}")
            t0 = self.final_at.pop(turn, None)
            llm_ms = int((time.monotonic() - t0) * 1000) if t0 else None
            if llm_ms is not None:
                self.stats["llm_ms"].append(llm_ms)
            await self._deliver(turn, text, llm_ms)
        elif t == "Termination":
            self.emit("session", id=None, model=None)
        elif t == "Error" or "error" in m:
            self.emit("error", message=f"STT: {m.get('error') or m}")

    @property
    def socket_route(self):
        return self.cfg["llm_model"] in SOCKET_MODELS

    async def _deliver(self, turn, text, llm_ms):
        if turn in self.timing and llm_ms is not None:
            self.timing[turn]["llm_ms"] = llm_ms
        text = text.strip().strip('"').strip()
        speaker = self.turn_speaker.pop(turn, None)
        if text.strip("()[]. ").upper() == "WAIT":  # the LLM chose to hear more before explaining
            self.timing.pop(turn, None)
            self.release_slot(turn)
            self.emit("translation", turn=turn, text="", llm_ms=llm_ms, speaker=speaker, waiting=True)
            return
        self.emit("translation", turn=turn, text=text, llm_ms=llm_ms, speaker=speaker)
        if not text:
            self.release_slot(turn)
            return
        slot = self.turn_slot.pop(turn, None)
        if slot is None or slot not in self.slots:
            slot = self.reserve_slot(turn)
        tts = await self._voice_session(self._voice_for(speaker))
        await tts.speak(text, turn, slot)

    # ---- playback queue: translations are heard one after another, in order, never overlapping
    def reserve_slot(self, turn):
        sid = self.next_slot
        self.next_slot += 1
        self.slots[sid] = {"turn": turn, "t_sent": time.monotonic(), "chunks": [], "done": False, "started": False,
                           "played": 0.0, "trimmed": False}
        self.slot_order.append(sid)
        return sid

    def release_slot(self, turn):
        """Give up a reserved place in line (no translation coming), so it doesn't hold up the turns after it."""
        sid = self.turn_slot.pop(turn, None)
        if sid in self.slots:
            self.slots[sid]["done"] = True
            self._pump()

    def slot_sent(self, sid):
        if sid in self.slots:
            self.slots[sid]["t_sent"] = time.monotonic()
            self.slots[sid]["sent"] = True

    def tts_audio(self, sid, pcm, rate):
        slot = self.slots.get(sid)
        if slot is None:
            return
        if not slot["trimmed"]:  # drop the silence before the first word
            loud = np.flatnonzero(np.abs(pcm) > 0.01)
            if len(loud) == 0:
                return
            pcm = pcm[max(0, loud[0] - int(rate * 0.02)):].copy()
            f = min(len(pcm), int(rate * 0.005))
            pcm[:f] *= np.linspace(0, 1, f, dtype=np.float32)  # no click where the cut lands
            slot["trimmed"] = True
            slot["t_first"] = time.monotonic()
        slot["chunks"].append((pcm, rate))
        self._pump()

    def tts_done(self, sid):
        if sid in self.slots:
            self.slots[sid]["done"] = True
            self._pump()

    def _pump(self):
        """Feed the players from the slot at the head of the line only; later slots wait their turn.

        While earlier audio is still playing, a sentence is held until it has fully arrived; then, if we're behind,
        it's sped up as a whole (pitch kept, long pauses shortened), which can't be done chunk by chunk."""
        while self.slot_order:
            sid = self.slot_order[0]
            slot = self.slots[sid]
            if not slot["done"] and not slot["chunks"] and time.monotonic() - slot["t_sent"] > (15 if slot.get("sent") else 25):
                slot["done"] = True  # a request that never answered must not block the rest
            backlog = self.players[0].queued_seconds if self.players else 0.0
            if not slot["started"] and slot["chunks"] and not slot["done"] and backlog > 0.6:
                return  # earlier audio still has a while to play: wait for the whole sentence
            if slot["chunks"] and not slot["started"]:
                slot["started"] = True
                self.on_voice_start(slot["turn"], slot["t_sent"], slot.get("t_first"))
                if slot["done"] and backlog > 0.3:
                    rate = slot["chunks"][0][1]
                    pcm = np.concatenate([c for c, _ in slot["chunks"]])
                    ahead = sum(len(c) / r for o in list(self.slot_order)[1:] for c, r in self.slots[o]["chunks"])
                    total = backlog + len(pcm) / rate + ahead
                    speed = min(1.4, 1.0 + 0.12 * max(0.0, total - 1.5))
                    slot["chunks"] = [(stretch(pcm, rate, speed), rate)]
                    slot["speed"] = round(speed, 2)
            while slot["chunks"]:
                pcm, rate = slot["chunks"].pop(0)
                slot["played"] += self.play(pcm, rate)
            if not slot["done"]:
                return
            for p in self.players:
                p.end_slot()
            if slot["started"]:
                self.on_voice_end(slot["turn"], slot["played"])
            self.slot_order.popleft()
            del self.slots[sid]

    def _voice_for(self, speaker):
        """The lane's voice for the first speaker; each new speaker gets the next voice for the target language,
        then voices recorded in other languages (TTS speaks them in the target language)."""
        main = self.cfg["voice"]
        if not speaker:
            return main
        chosen = (self.cfg.get("speaker_voices") or {}).get(speaker)
        if chosen:
            if self.speaker_voice.get(speaker) != chosen:
                self.speaker_voice[speaker] = chosen
                self.emit("speaker", speaker=speaker, voice=chosen)
            return chosen
        if speaker not in self.speaker_voice:
            target = self.cfg["target"]
            pool = [main] + [v for v, _ in VOICES.get(target, []) if v != main]
            pool += [v for lang, vs in VOICES.items() if lang != target for v, _ in vs if v not in pool]
            used = set(self.speaker_voice.values())
            self.speaker_voice[speaker] = next((v for v in pool if v not in used), pool[len(self.speaker_voice) % len(pool)])
            self.emit("speaker", speaker=speaker, voice=self.speaker_voice[speaker])
        return self.speaker_voice[speaker]

    async def _voice_session(self, voice):
        if voice not in self.voices:
            tts = TTS(self, voice, self.cfg["target"], self.tts_rate)
            await tts.connect()
            self.voices[voice] = tts
        return self.voices[voice]

    async def _direct_llm(self, turn, transcript):
        """Call LLM Gateway ourselves, for models the socket's llm_gateway doesn't serve."""
        import re

        import aiohttp

        prompt = render_prompt(self.cfg).replace("{{turn}}", transcript)
        prompt = re.sub(r"\{\{history_(\d+)\}\}", lambda mm: " ".join(self.history[-int(mm.group(1)):]), prompt)
        body = {"model": self.cfg["llm_model"], "messages": [{"role": "user", "content": prompt}], "max_tokens": 400}
        t0 = self.final_at.get(turn, time.monotonic())
        try:
            if self.http is None or self.http.closed:
                self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), headers={"User-Agent": USER_AGENT})
            async with self.http.post(GATEWAY_URL, json=body, headers={"Authorization": api_key()}) as r:
                data = await r.json()
            if r.status >= 400:
                raise RuntimeError(f"{r.status} {json.dumps(data)[:200]}")
            text = data["choices"][0]["message"]["content"] or ""
        except Exception as e:  # noqa: BLE001
            self.final_at.pop(turn, None)
            self.release_slot(turn)
            self.emit("error", message=f"LLM ({self.cfg['llm_model']}): {e}")
            return
        speaker = self.turn_speaker.get(turn)
        self.history = (self.history + [f"Speaker {speaker}: {transcript}" if speaker else transcript])[-10:]
        self.final_at.pop(turn, None)
        llm_ms = int((time.monotonic() - t0) * 1000)
        self.stats["llm_ms"].append(llm_ms)
        if self.running:
            await self._deliver(turn, text, llm_ms)

    def _drop_repeat(self, turn, m):
        """After a forced cut the server can transcribe the cut word again as the start of the next turn
        ("...15 de diciembre." then "diciembre."). Drop words that repeat the end of the previous turn."""
        norm = lambda w: re.sub(r"[^\w']", "", w.lower())
        words = m.get("words") or []
        toks = [norm(w["text"]) for w in words]
        prev, self.last_words = self.last_words, ([norm(w["text"]) for w in words] or self.last_words)[-8:]
        k = next((k for k in range(min(len(prev), len(toks)), 0, -1) if prev[-k:] == toks[:k]), 0)
        if k == 0:
            return False
        if k == len(toks):  # nothing new
            self.split_turns.add(turn)
            return True
        rest = words[k:]
        m["words"], m["transcript"] = rest, " ".join(w["text"] for w in rest)
        if self.socket_route:  # the socket translates the untrimmed text, so translate the trimmed text ourselves
            self.split_turns.add(turn)
            m["_direct"] = True
        return False

    def _split_by_speaker(self, turn, m):
        """A turn where the speaker changed part way: translate each speaker's words separately, in their own
        voice. Final words carry a speaker label; partials don't, so this is done when the turn ends."""
        if not self.cfg.get("diarize"):
            return False
        runs = []
        for w in m.get("words") or []:
            spk = w.get("speaker")
            spk = spk if spk and str(spk).isalpha() and spk not in ("PENDING", "UNKNOWN") else (runs[-1][0] if runs else None)
            if runs and runs[-1][0] == spk:
                runs[-1][1].append(w)
            else:
                runs.append([spk, [w]])
        for i in range(len(runs) - 2, -1, -1):  # leading words arrive unlabelled: give them the next speaker
            if runs[i][0] is None:
                runs[i][0] = runs[i + 1][0]
        merged = []
        for spk, ws in runs:  # fold one-word blips into their neighbour
            if merged and (len(ws) < 2 or merged[-1][0] == spk):
                merged[-1][1].extend(ws)
            else:
                merged.append([spk, ws])
        if len(merged) < 2:
            return False
        now = time.monotonic()
        self.split_turns.add(turn)
        wall = lambda ms: self.anchor[1] - (self.anchor[0] - ms) / 1000
        for i, (spk, ws) in enumerate(merged):
            sub = f"{turn}.{i}"
            text = " ".join(w["text"] for w in ws)
            end = wall(ws[-1]["end"])
            self.timing[sub] = {"speech_start": wall(ws[0]["start"]), "speech_end": end,
                                "detect_ms": max(0, int((now - end) * 1000))}
            self.turn_speaker[sub] = spk
            self.final_at[sub] = now
            self.stats["turns"] += 1
            self.emit("final", turn=sub, text=text, speaker=spk)
            asyncio.create_task(self._direct_llm(sub, text))
        return True

    def _maybe_segment(self, turn, text, words=()):
        """Continuous speech (a video, a fast talker) may never pause long enough to end a turn. End it ourselves at
        the first sentence end after SEGMENT_AFTER_S, or at SEGMENT_MAX_S, so translation keeps up."""
        if not self.cfg.get("auto_segment", True) or turn in self.forced or self.ws is None or not text.strip():
            return
        dur = time.monotonic() - self.turn_started.setdefault(turn, time.monotonic())
        speakers = {w.get("speaker") for w in words if w.get("word_is_final") and str(w.get("speaker", "")).isalpha()
                    and w.get("speaker") not in ("PENDING", "UNKNOWN")}
        if len(speakers) > 1 or (dur >= SEGMENT_AFTER_S and SENTENCE_END.search(text)) or dur >= SEGMENT_MAX_S:
            self.forced.add(turn)
            asyncio.create_task(self._force_endpoint())

    async def _force_endpoint(self):
        try:
            await self.ws.send(json.dumps({"type": "ForceEndpoint"}))
        except Exception:  # noqa: BLE001
            pass

    def on_voice_start(self, turn, t_sent, t_first=None):
        """First translated audio for a turn: work out how far behind real time it will actually be heard."""
        now = time.monotonic()
        tts_ms = int(((t_first or now) - t_sent) * 1000)
        queue = max((p.queued_seconds for p in self.players), default=0.0)  # earlier translations still playing
        timing = self.timing.get(turn) if turn is not None else None
        if not timing:
            self.emit("tts_start", turn=turn, tts_ms=tts_ms)
            return
        timing["play_at"] = now + queue  # when the translated voice is actually heard
        self.emit("lag", turn=turn, lag_ms=int((timing["play_at"] - timing["speech_end"]) * 1000),
                  start_ms=int((timing["play_at"] - timing["speech_start"]) * 1000),
                  detect_ms=timing["detect_ms"], llm_ms=timing.get("llm_ms"), tts_ms=tts_ms,
                  queue_ms=int(queue * 1000), t=round(now - self.t_start, 2))

    def on_voice_end(self, turn, seconds):
        timing = self.timing.pop(turn, None) if turn is not None else None
        if timing and "play_at" in timing:
            end = timing["play_at"] + seconds
            self.emit("lag_end", turn=turn, end_ms=int((end - timing["speech_end"]) * 1000),
                      voice_ms=int(seconds * 1000))

    def _check_translated(self, turn):
        if self.running and self.final_at.pop(turn, None) is not None:
            self.release_slot(turn)
            self.emit("error", message=f"No translation came back for that turn from {self.cfg['llm_model']}. "
                                       "If this keeps happening, pick another LLM model (Gemini 2.5 Flash Lite works).")

    async def skip(self):
        self.slots.clear()
        self.slot_order.clear()
        self.turn_slot.clear()
        for p in self.players:
            p.clear()
        for tts in self.voices.values():
            await tts.cancel()

    def _route_system_mic(self):
        """Make the virtual mic the Mac's default input, so any app on 'Same as System' hears the translation."""
        current = default_input_name()
        if is_virtual(current):
            return
        if set_default_input(self.cfg["output"]):
            self.switched_mic = current
            self.hub.cfg["restore_input"] = current  # survives a crash; restored at next launch
            save_config(self.hub.cfg)
            self.emit("notice", message=f"Your Mac's microphone is now the translated voice. {current} is restored when you stop.")

    def _route_system_output(self, device):
        """Send the Mac's sound into the silent device (that's the muting). Saved first, so a crash can undo it."""
        current = default_output_name()
        if not is_virtual(current):
            self.hub.cfg["restore_output"] = current
            save_config(self.hub.cfg)
            self.switched_out = current
        if not set_default_output(device):
            raise RuntimeError(f"couldn't route the Mac's sound to {device}")
        print(f"mute: output {current} -> {device}", flush=True)

    def _restore_system_output(self):
        target = getattr(self, "switched_out", None) or self.hub.cfg.get("restore_output")
        if target and is_virtual(default_output_name()):
            ok = set_default_output(target)
            print(f"mute: output restored to {target}: {ok}", flush=True)
        self.switched_out = None
        if self.hub.cfg.pop("restore_output", None) is not None:
            save_config(self.hub.cfg)

    def _restore_system_mic(self):
        if self.switched_mic:
            set_default_input(self.switched_mic)
            self.switched_mic = None
            self.hub.cfg.pop("restore_input", None)
            save_config(self.hub.cfg)

    HOT_KEYS = {"target", "source", "voice", "prompt", "preset", "llm_model", "stt_model", "mode", "diarize",
                "max_speakers", "speaker_voices", "auto_segment", "style", "style_note", "voices_by_lang"}

    async def retarget(self, cfg):
        """Switch language, voice or model while running: reconnect only the STT and TTS sessions. The audio
        source, the players, the muted original and the default-mic routing stay exactly as they are, so nothing
        flips back to the untranslated audio for a moment."""
        lang_changed = cfg.get("target") != self.cfg.get("target")
        self.cfg = dict(cfg)
        if lang_changed:  # TTS sessions are per language
            for tts in list(self.voices.values()):
                await tts.close()
            self.voices, self.speaker_voice = {}, {}
            self.history = []
        self.tts = await self._voice_session(self.cfg["voice"])
        if self.ws is not None:
            await self.ws.close()  # the session loop reconnects at once with the new settings
        self.emit("status", running=True, input=self.source.name if self.source else "", output=[p.name for p in self.players])

    async def _teardown(self):
        self._restore_system_output()  # first: unmuting must happen even if anything below fails
        self._restore_system_mic()
        if self.http is not None:
            await self.http.close()
            self.http = None
        if self.source:
            await asyncio.to_thread(self.source.stop)
            self.source = None
        for tts in list(self.voices.values()):
            await tts.close()
        self.voices = {}
        self.tts = None
        for p in self.players:
            p.close()
        self.players = []

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self.task = None
        await self._teardown()
        self.emit("status", running=False)


# ---------------------------------------------------------------- hub + HTTP
class Hub:
    def __init__(self):
        self.cfg = load_config()
        self.clients = set()
        self.lanes = {k: Lane(k, self) for k in self.cfg["lanes"]}

    def broadcast(self, msg):
        data = json.dumps(msg, ensure_ascii=False)
        for ws in list(self.clients):
            asyncio.ensure_future(self._send(ws, data))

    async def _send(self, ws, data):
        try:
            await ws.send_str(data)
        except Exception:  # noqa: BLE001
            self.clients.discard(ws)


hub = Hub()
routes = web.RouteTableDef()


@routes.get("/")
async def index(_req):
    return web.FileResponse(HERE / "static" / "index.html")


@routes.get("/captions")
async def captions(_req):
    return web.FileResponse(HERE / "static" / "captions.html")


@routes.get("/api/state")
async def state(_req):
    if not any(l.running for l in hub.lanes.values()):
        sd._terminate()  # pick up devices plugged in since start (AirPods etc.)
        sd._initialize()
    return web.json_response({
        "config": hub.cfg, "devices": list_devices(), "system_input": SYSTEM_INPUT, "mute_device": mute_device(),
        "has_key": bool(api_key()),
        "system_capture": SYSCAP.exists(), "running": {k: l.running for k, l in hub.lanes.items()},
        "options": {"tts_languages": TTS_LANGUAGES, "source_languages": SOURCE_LANGUAGES, "voices": VOICES,
                    "stt_models": STT_MODELS, "llm_models": LLM_MODELS, "presets": PRESETS}})


@routes.get("/api/lanes")
async def lanes_state(_req):
    """Small status for the menu bar."""
    return web.json_response({k: {"running": l.running, "target": hub.cfg["lanes"][k]["target"],
                                  "source": hub.cfg["lanes"][k]["source"]} for k, l in hub.lanes.items()})


@routes.post("/api/key")
async def set_key(req):
    """First run: check the key against the API, then save it (chmod 600) in the data folder."""
    import aiohttp
    key = ((await req.json()).get("key") or "").strip()
    if not key:
        return web.json_response({"ok": False, "error": "Paste your AssemblyAI API key."}, status=400)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), headers={"User-Agent": USER_AGENT}) as s:
            async with s.get("https://api.assemblyai.com/v2/transcript?limit=1", headers={"Authorization": key}) as r:
                if r.status in (401, 403):
                    return web.json_response({"ok": False, "error": "That key wasn't accepted. Check it and try again."}, status=400)
    except Exception:  # noqa: BLE001  offline: save it anyway, the first session will tell
        pass
    try:
        save_api_key(key)
    except Exception as e:  # noqa: BLE001
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    return web.json_response({"ok": True})


@routes.post("/api/restore-sound")
async def restore_sound_route(_req):
    """Escape hatch: stop both directions and put the Mac's output and mic back on real devices."""
    for lane in hub.lanes.values():
        if lane.running:
            await lane.stop()
    return web.json_response({"ok": True, "restored": restore_sound()})


@routes.post("/api/prefs")
async def set_prefs(req):
    """App-wide preferences, e.g. favourite voices."""
    body = await req.json()
    for k in ("favorite_voices",):
        if k in body:
            hub.cfg[k] = body[k]
    save_config(hub.cfg)
    return web.json_response({"ok": True})


@routes.post("/api/config")
async def set_config(req):
    body = await req.json()
    key, lane_cfg = body["lane"], body["config"]
    lc = hub.cfg["lanes"][key]
    if "voice" in lane_cfg:  # remember the voice picked for this language
        lang = lane_cfg.get("target", lc.get("target"))
        lc.setdefault("voices_by_lang", {})[lang] = lane_cfg["voice"]
    lc.update(lane_cfg)
    save_config(hub.cfg)
    lane = hub.lanes[key]
    if lane.running and not body.get("restart", True):  # live change (voices): no reconnect
        lane.cfg.update(lane_cfg)
        if "voice" in lane_cfg:  # speakers without a chosen voice get re-assigned around the new main voice
            lane.speaker_voice = {s: v for s, v in lane.speaker_voice.items() if s in (lane.cfg.get("speaker_voices") or {})}
    if lane.running and body.get("restart", True):
        try:
            if set(lane_cfg) <= Lane.HOT_KEYS:
                await lane.retarget(hub.cfg["lanes"][key])
            else:
                await lane.start(hub.cfg["lanes"][key])
        except Exception as e:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(e)}, status=400)
    return web.json_response({"ok": True})


@routes.post("/api/start/{lane}")
async def start(req):
    lane = hub.lanes[req.match_info["lane"]]
    if not api_key():
        return web.json_response({"ok": False, "error": "Add your AssemblyAI API key first (Settings)."}, status=400)
    try:
        await lane.start(hub.cfg["lanes"][lane.key])
    except Exception as e:  # noqa: BLE001
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    return web.json_response({"ok": True})


@routes.post("/api/stop/{lane}")
async def stop(req):
    await hub.lanes[req.match_info["lane"]].stop()
    return web.json_response({"ok": True})


@routes.post("/api/skip/{lane}")
async def skip(req):
    await hub.lanes[req.match_info["lane"]].skip()
    return web.json_response({"ok": True})


@routes.post("/api/speak/{lane}")
async def speak(req):
    """Type-to-speak: send text straight to the lane's voice (or translate it first with translate=true)."""
    lane = hub.lanes[req.match_info["lane"]]
    body = await req.json()
    if not lane.running:
        return web.json_response({"ok": False, "error": "start the lane first"}, status=400)
    await lane.tts.speak(body["text"], None, lane.reserve_slot(None))
    lane.emit("translation", turn=None, text=body["text"], llm_ms=None, typed=True)
    return web.json_response({"ok": True})


@routes.post("/api/test/system")
async def test_system(_req):
    """Play a sound from another process and check the system tap hears it (checks the permission)."""
    peaks = []
    src = SystemSource(lambda x, r: peaks.append(float(np.abs(x).max()) if len(x) else 0.0), lambda s: None)
    try:
        await asyncio.to_thread(src.start)
        await asyncio.sleep(0.3)
        p = await asyncio.create_subprocess_exec("afplay", "-v", "0.4", "/System/Library/Sounds/Glass.aiff")
        await p.wait()
        await asyncio.sleep(0.4)
    except Exception as e:  # noqa: BLE001
        return web.json_response({"ok": False, "error": str(e)})
    finally:
        await asyncio.to_thread(src.stop)
    peak = max(peaks) if peaks else 0.0
    return web.json_response({"ok": peak > 0.002, "peak": peak, "chunks": len(peaks)})


@routes.post("/api/quit")
async def quit_app(_req):
    asyncio.get_running_loop().call_later(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM))
    return web.json_response({"ok": True})


@routes.get("/ws")
async def ui_socket(req):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(req)
    hub.clients.add(ws)
    try:
        async for _ in ws:
            pass
    finally:
        hub.clients.discard(ws)
    return ws


async def on_shutdown(_app):
    for lane in hub.lanes.values():
        await lane.stop()
    for ws in list(hub.clients):  # open windows would otherwise hold shutdown for aiohttp's 60 s grace period
        await ws.close()


def open_window(url):
    chrome = "/Applications/Google Chrome.app"
    if os.path.exists(chrome):  # a chromeless app window
        subprocess.Popen(["open", "-na", chrome, "--args", f"--app={url}", "--window-size=600,900"])
    else:
        subprocess.Popen(["open", url])


def main():
    if not api_key():
        print("no AssemblyAI API key yet: the window will ask for one", flush=True)
    fixed = restore_sound()  # the app quit without restoring output/mic last time
    if fixed:
        print("restored at launch:", ", ".join(fixed), flush=True)
    app = web.Application()
    app.add_routes(routes)
    app.on_shutdown.append(on_shutdown)
    url = f"http://127.0.0.1:{PORT}"
    parent = os.getppid()

    def watch_parent():  # quit with the menu bar app, even if it was killed
        while True:
            time.sleep(1)
            if os.getppid() != parent:
                os.kill(os.getpid(), signal.SIGTERM)
                return

    threading.Thread(target=watch_parent, daemon=True).start()
    if "--no-window" not in sys.argv:
        threading.Timer(0.8, open_window, args=(url,)).start()
    print(f"Natter on {url}", flush=True)
    web.run_app(app, host="127.0.0.1", port=PORT, print=None, shutdown_timeout=2)


if __name__ == "__main__":
    main()
