"""HomePod Link - stream Windows system audio to a HomePod over AirPlay 2.

Captures whatever the PC is playing (WASAPI loopback on the default output)
and sends it to the chosen HomePod using pyatv's AirPlay 2 (HAP transient
pairing, real-time PCM stream).
"""

import asyncio
import json
import logging
import os
import platform
import queue
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk

import numpy as np
import pyatv
import pyatv.protocols.raop as raop_mod
from pyatv.const import Protocol
from pyatv.settings import AirPlayVersion
from pyatv.protocols.raop.audio_source import AudioSource
from pyatv.protocols.raop.protocols import StreamContext
from pyatv.storage.memory_storage import MemoryStorage
from pyatv.support.metadata import MediaMetadata

APP_NAME = "HomePod Link"
APP_VERSION = "1.3.0"
BUILD_NUMBER = 0  # stamped by the GitHub build
REPO = "danielrstrickland-cell/Homepod-link"
REPORT_TOKEN = ""  # stamped by the GitHub build from the REPORT_TOKEN secret (issues-only key)
SAMPLE_RATE = 44100
CHANNELS = 2
BYTES_PER_SAMPLE = 2
FRAME_BYTES = CHANNELS * BYTES_PER_SAMPLE

SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "HomePodLink")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")
LOG_FILE = os.path.join(SETTINGS_DIR, "homepod_link.log")

DEFAULTS = {
    "latency_ms": 250,
    "volume": 50,
    "protocol": "AirPlay 2",
    "last_devices": [],
    "stereo_split": False,
    "swap_lr": False,
    "rainbow": True,
    "auto_fix": True,
    "send_reports": True,
    "eq_on": False,
    "smart_eq": False,
    "eq_bands": 10,
    "eq_preset": "Flat",
    "eq_gains": None,
    "auto_sim": True,
    "resume_after_update": False,
    "viz_height": 110,
    "viz_style": "Bars",
    "viz_colors": "Theme",
    "source": None,  # None = not chosen yet (auto-pick Line 1 if present)
    "settings_rev": 2,
}

# ---------------------------------------------------------------- settings --


def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = {**DEFAULTS, **data}
        if data.get("settings_rev", 1) < 2:
            # v0.6 cut the default delay; move old 500 ms installs to the new default
            merged["latency_ms"] = min(merged["latency_ms"], DEFAULTS["latency_ms"])
            merged["settings_rev"] = 2
        return merged
    except Exception:
        return dict(DEFAULTS)


def save_settings(s):
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


# ------------------------------------------------------------ latency patch --
# pyatv hard-codes ~1.5 s of latency. We override it with the user's choice.
# AirPlay 2 receivers advertise a minimum of 11025 samples (250 ms).

LATENCY_MS = {"value": DEFAULTS["latency_ms"]}
MIN_DELAY_MS = 100
_orig_reset = StreamContext.reset


def _patched_reset(self):
    _orig_reset(self)
    ms = max(MIN_DELAY_MS, min(2000, int(LATENCY_MS["value"])))
    self.latency = int(self.sample_rate * ms / 1000)


StreamContext.reset = _patched_reset

# pyatv tells the HomePod we never go below 250 ms. Advertise our real minimum.
from pyatv.support.rtsp import RtspSession  # noqa: E402

_orig_rtsp_setup = RtspSession.setup


async def _patched_rtsp_setup(self, *args, **kwargs):
    body = kwargs.get("body")
    if isinstance(body, dict):
        for st in body.get("streams", []):
            if "latencyMin" in st:
                st["latencyMin"] = int(SAMPLE_RATE * MIN_DELAY_MS / 1000)
    return await _orig_rtsp_setup(self, *args, **kwargs)


RtspSession.setup = _patched_rtsp_setup

# Security: AirPlay 2 encrypts every audio packet (ChaCha20-Poly1305, key from the
# per-session X25519 pairing). Refuse to send anything if encryption isn't active.
from pyatv.protocols.raop.protocols import airplayv2 as _ap2  # noqa: E402

_orig_send_audio = _ap2.AirPlayV2.send_audio_packet


async def _encrypted_only(self, transport, rtp_header, audio):
    if not getattr(self, "_cipher", None):
        raise RuntimeError("Refusing to stream: audio encryption is not active")
    return await _orig_send_audio(self, transport, rtp_header, audio)


_ap2.AirPlayV2.send_audio_packet = _encrypted_only

# ------------------------------------------------------------- audio source --


class Timeline:
    """One shared, time-stamped ring of captured PC audio.

    Every speaker reads the slice that matches the wall clock (minus a small
    safety delay), so all speakers play the same moment at the same time no
    matter when each one connected."""

    SECONDS = 8
    READ_DELAY = 0.05  # read audio captured ~50 ms ago (auto-raised if it arrives late)
    SNAP = 0.25  # only jump if more than 250 ms off (after silence, hiccups)
    NUDGE = 2  # otherwise correct gently: at most 2 samples per block (inaudible)
    DEADBAND = 0.012  # ignore normal jitter under 12 ms

    def __init__(self):
        self.size = SAMPLE_RATE * self.SECONDS
        self.ring = np.zeros((self.size, CHANNELS), dtype=np.float32)
        self.lock = threading.Lock()
        self.t_ref = time.monotonic()
        self.write_pos = None
        self.writer_resyncs = 0

    def now_pos(self, t=None):
        return int(((t if t is not None else time.monotonic()) - self.t_ref) * SAMPLE_RATE)

    def _zero(self, start, end):
        if end <= start:
            return
        if end - start >= self.size:
            self.ring[:] = 0
            return
        a, b = start % self.size, end % self.size
        if a < b:
            self.ring[a:b] = 0
        else:
            self.ring[a:] = 0
            self.ring[:b] = 0

    def push(self, data, t_recv):
        n = len(data)
        expected = self.now_pos(t_recv) - n
        with self.lock:
            if self.write_pos is None or abs(self.write_pos - expected) > self.SNAP * SAMPLE_RATE:
                if self.write_pos is not None and expected > self.write_pos:
                    self._zero(self.write_pos, expected)  # silence gap
                if self.write_pos is not None:
                    self.writer_resyncs += 1
                self.write_pos = expected
            else:
                # Track the clock slowly so capture jitter never causes a jump.
                self._err = 0.98 * getattr(self, "_err", 0.0) + 0.02 * (expected - self.write_pos)
                step = 0
                if abs(self._err) > self.DEADBAND * SAMPLE_RATE:
                    step = int(max(-self.NUDGE, min(self.NUDGE, round(self._err))))
                if step > 0:
                    self._zero(self.write_pos, self.write_pos + step)
                self.write_pos += step
                self._err -= step
            a = self.write_pos % self.size
            first = min(n, self.size - a)
            self.ring[a:a + first] = data[:first]
            if first < n:
                self.ring[: n - first] = data[first:]
            self.write_pos += n

    def read(self, pos, n):
        """Return n frames starting at pos (zeros where nothing was captured)."""
        out = np.zeros((n, CHANNELS), dtype=np.float32)
        with self.lock:
            wp = self.write_pos
            if wp is None:
                return out, 0
            oldest = wp - self.size + SAMPLE_RATE  # keep a 1 s guard from the writer
            lo, hi = max(pos, oldest), min(pos + n, wp)
            if hi <= lo:
                return out, 0
            a, b = lo % self.size, hi % self.size
            if a < b:
                out[lo - pos:hi - pos] = self.ring[a:b]
            else:
                k = self.size - a
                out[lo - pos:lo - pos + k] = self.ring[a:]
                out[lo - pos + k:hi - pos] = self.ring[:b]
            return out, hi - lo


TIMELINE = Timeline()


class LiveBuffer:
    """Per-speaker reader on the shared timeline (keeps the old name/interface)."""

    def __init__(self, channel="both"):
        self.channel = channel  # "both", "left" or "right"
        self.pos = None
        self.dropped = 0  # resyncs (times we jumped to stay in time)
        self.underruns = 0  # reads where audio hadn't arrived yet
        self.captured = 0  # frames of real audio sent

    def clear(self):
        self.pos = None

    def pop(self, nbytes: int) -> bytes:
        n = nbytes // FRAME_BYTES
        target = TIMELINE.now_pos() - int(Timeline.READ_DELAY * SAMPLE_RATE)
        if self.pos is None or abs(self.pos - target) > Timeline.SNAP * SAMPLE_RATE:
            if self.pos is not None:
                self.dropped += 1
            self.pos = target
            self._err = 0.0
        else:
            # Sender timing is bursty; follow the clock smoothly instead of jumping.
            self._err = 0.95 * getattr(self, "_err", 0.0) + 0.05 * (target - self.pos)
            step = 0
            if abs(self._err) > Timeline.DEADBAND * SAMPLE_RATE:
                step = int(max(-Timeline.NUDGE, min(Timeline.NUDGE, round(self._err))))
            self.pos += step
            self._err -= step
        data, got = TIMELINE.read(self.pos, n)
        self.pos += n
        if got == 0:
            pass  # PC is silent - send silence
        elif got < n:
            self.underruns += 1
        self.captured += got
        if self.channel == "left":
            data = np.column_stack([data[:, 0], data[:, 0]])
        elif self.channel == "right":
            data = np.column_stack([data[:, 1], data[:, 1]])
        # AirPlay wants big-endian 16-bit PCM
        return (np.clip(data, -1.0, 1.0) * 32767).astype(">i2").tobytes()


class LiveSource(AudioSource):
    """Endless PCM source fed by the loopback capture."""

    def __init__(self, buf: LiveBuffer):
        self.buf = buf

    async def readframes(self, nframes: int) -> bytes:
        return self.buf.pop(nframes * FRAME_BYTES)

    async def get_metadata(self) -> MediaMetadata:
        return MediaMetadata(title="PC audio", artist=platform.node() or "Windows PC")

    @property
    def sample_rate(self) -> int:
        return SAMPLE_RATE

    @property
    def channels(self) -> int:
        return CHANNELS

    @property
    def sample_size(self) -> int:
        return BYTES_PER_SAMPLE

    @property
    def duration(self) -> int:
        return 0


LIVE_SENTINEL = "__homepod_link_live__:"
_orig_open_source = raop_mod.open_source
_live_sources = {}  # speaker name -> LiveSource


async def _patched_open_source(source, sample_rate, channels, sample_size):
    if isinstance(source, str) and source.startswith(LIVE_SENTINEL):
        return _live_sources[source[len(LIVE_SENTINEL):]]
    return await _orig_open_source(source, sample_rate, channels, sample_size)


raop_mod.open_source = _patched_open_source

# Count lost-audio reports from each speaker (they ask us to resend packets).
from pyatv.protocols.raop import stream_client as _sc  # noqa: E402

LOST = {}  # speaker IP -> lost packet count
_orig_retx = _sc.ControlClient._retransmit_lost_packets


def _counting_retx(self, request, addr):
    LOST[addr[0]] = LOST.get(addr[0], 0) + request.lost_packets
    return _orig_retx(self, request, addr)


_sc.ControlClient._retransmit_lost_packets = _counting_retx

# ------------------------------------------------------------------ capture --


class LoopbackCapture(threading.Thread):
    """Records the chosen source (default output via WASAPI loopback, or a
    specific device such as Virtual Audio Cable "Line 1") into the timeline."""

    def __init__(self, log, source=""):
        super().__init__(daemon=True)
        self.log = log
        self._stop_evt = threading.Event()
        self._restart = threading.Event()
        self._lock = threading.Lock()
        self.buffers = []
        self.device_name = "?"
        self.source = source  # "" = default output (everything); else a device name

    def set_source(self, name):
        if name != self.source:
            self.source = name
            self._restart.set()

    def _open(self, sc):
        """Pick the capture device: a named source (e.g. Line 1) or default output."""
        if self.source:
            for m in sc.all_microphones(include_loopback=True):
                if source_label(m) == self.source:
                    return m, self.source
            self.log(f"Source '{self.source}' not found - using default output for now")
        speaker = sc.default_speaker()
        return sc.get_microphone(id=str(speaker.name), include_loopback=True), speaker.name

    def add(self, buf):
        with self._lock:
            self.buffers.append(buf)

    def remove(self, buf):
        with self._lock:
            if buf in self.buffers:
                self.buffers.remove(buf)

    def stop(self):
        self._stop_evt.set()

    def _fan_out(self, data):
        t = time.monotonic()
        TIMELINE.push(EQ.process(data.astype(np.float32)), t)

    def run(self):
        try:
            import soundcard as sc  # imported here so errors land in the log
        except Exception as ex:
            self.log(f"Audio capture library failed to load: {ex}")
            return
        while not self._stop_evt.is_set():
            try:
                self._restart.clear()
                mic, label = self._open(sc)
                following_default = not self.source or label == sc.default_speaker().name
                self.device_name = sc.default_speaker().name
                self.log(f"Capturing from: {label}")
                block = 220  # 5 ms blocks keep capture latency low
                with mic.recorder(samplerate=SAMPLE_RATE, channels=CHANNELS, blocksize=block) as rec:
                    checks = 0
                    while not self._stop_evt.is_set():
                        data = rec.record(numframes=block)
                        if data.ndim == 1:
                            data = np.column_stack([data, data])
                        elif data.shape[1] == 1:
                            data = np.repeat(data, 2, axis=1)
                        elif data.shape[1] > 2:
                            data = data[:, :2]
                        self._fan_out(data)
                        checks += 1
                        if self._restart.is_set():
                            self.log("Audio source changed, restarting capture")
                            break
                        if checks >= 200:  # about once a second
                            checks = 0
                            if following_default and sc.default_speaker().name != self.device_name:
                                self.log("Default output changed, restarting capture")
                                break
            except Exception as ex:
                self.log(f"Capture error: {ex!r} - retrying in 2 s")
                time.sleep(2)


def source_label(m):
    return f"{m.name} (what it plays)" if getattr(m, "isloopback", False) else f"{m.name} (input)"


def list_sources():
    """Capture devices: inputs (e.g. Line 1) and loopbacks of every output."""
    try:
        import soundcard as sc

        return [source_label(m) for m in sc.all_microphones(include_loopback=True)]
    except Exception:
        return []


def find_line1(sources):
    """Prefer Virtual Audio Cable's Line 1 recording endpoint if present."""
    for lab in sources:
        if "line 1" in lab.lower() and lab.endswith("(input)"):
            return lab
    for lab in sources:
        if "line 1" in lab.lower():
            return lab
    return ""


# -------------------------------------------------------------- equalizer --

# Presets are curves over frequency (Hz -> dB), so they fit any band count.
PRESETS = {
    "Flat": [(20, 0), (20000, 0)],
    "Bass boost": [(20, 6), (60, 6), (150, 3), (300, 0), (20000, 0)],
    "Vocal": [(20, -2), (150, -2), (400, 0), (1200, 3), (3000, 3), (6000, 1), (20000, 0)],
    "Loudness": [(20, 5), (100, 4), (400, 0), (3000, 0), (8000, 3), (20000, 3)],
    "Night": [(20, -4), (100, -3), (300, 0), (2500, 2), (8000, 0), (20000, -2)],
    # Engine and kerb rumble up, boxy low-mids down, tyre scrub and spotter calls up.
    "Sim racing": [(20, 3), (45, 5), (90, 5), (180, 1), (300, -2), (500, -1),
                   (1500, 2), (2500, 3), (5500, 2), (10000, 0), (20000, -1)],
}
BAND_CHOICES = (5, 10, 16, 32)


def curve_db(points, f):
    xs = np.log10([p[0] for p in points])
    return float(np.interp(np.log10(f), xs, [p[1] for p in points]))


def band_freqs(n):
    return np.geomspace(31.5, 16000, n)


def peaking_sos(freqs, gains_db, q):
    """RBJ peaking biquads as an SOS array (one section per band)."""
    sos = []
    for f, g in zip(freqs, gains_db):
        a = 10 ** (g / 40)
        w = 2 * np.pi * f / SAMPLE_RATE
        alpha = np.sin(w) / (2 * q)
        b0, b1, b2 = 1 + alpha * a, -2 * np.cos(w), 1 - alpha * a
        a0, a1, a2 = 1 + alpha / a, -2 * np.cos(w), 1 - alpha / a
        sos.append([b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0])
    return np.array(sos)


class Equalizer:
    """Graphic EQ (5-32 bands) applied in the capture path with zero added delay,
    plus Smart EQ: a small on-device analyser that listens to the music, works out
    its tonal balance (bass-heavy, bright, vocal, speech...) and steers the EQ
    toward a balanced sound. Nothing leaves the PC."""

    def __init__(self):
        self.lock = threading.Lock()
        self.enabled = False
        self.smart = False
        self.n = 10
        self.manual = np.zeros(self.n)
        self.auto = np.zeros(self.n)
        self.style = ""
        self._an_buf = []
        self._an_frames = 0
        self._avg = None
        self._last_smart = 0.0
        self._rebuild()

    @property
    def gains(self):
        return self.auto if self.smart else self.manual

    def _rebuild(self, keep_state=False):
        freqs = band_freqs(self.n)
        bw = np.log2(16000 / 31.5) / max(1, self.n - 1)  # octaves between bands
        q = np.sqrt(2 ** bw) / (2 ** bw - 1)
        sos = peaking_sos(freqs, self.gains, q)
        pre = 10 ** (-0.5 * max(0.0, float(np.max(self.gains))) / 20)  # some headroom; peaks soft-limited
        with self.lock:
            if not keep_state or getattr(self, "zi", None) is None or self.zi.shape[1] != len(sos):
                from scipy.signal import sosfilt_zi

                z = sosfilt_zi(sos) * 0
                self.zi = np.stack([z, z])  # (channel, section, 2)
            self.sos, self.pre, self.freqs = sos, pre, freqs

    def set_bands(self, n):
        old_f, old_g = band_freqs(self.n), self.manual
        self.n = n
        new_f = band_freqs(n)
        self.manual = np.interp(np.log10(new_f), np.log10(old_f), old_g)
        self.auto = np.zeros(n)
        self._avg = None
        self._rebuild()

    def set_manual(self, gains):
        self.manual = np.clip(np.asarray(gains, dtype=float), -12, 12)
        self._rebuild(keep_state=True)

    def apply_preset(self, name):
        pts = PRESETS[name]
        self.set_manual([curve_db(pts, f) for f in band_freqs(self.n)])

    def process(self, data):
        """data: (frames, 2) float32. Returns equalised audio; updates Smart EQ."""
        if self.smart:
            self._analyse(data)
        if not self.enabled:
            return data
        from scipy.signal import sosfilt

        with self.lock:
            sos, pre = self.sos, self.pre
            out = np.empty_like(data)
            for ch in range(2):
                out[:, ch], self.zi[ch] = sosfilt(sos, data[:, ch] * pre, zi=self.zi[ch])
        # soft limiter: transparent below 0.8, rounds off peaks instead of hard clipping
        over = np.abs(out) > 0.8
        if over.any():
            out[over] = np.sign(out[over]) * (0.8 + 0.2 * np.tanh((np.abs(out[over]) - 0.8) / 0.2))
        return out

    # ---- Smart EQ -------------------------------------------------------
    def _analyse(self, data):
        self._an_buf.append(data[:, 0] + data[:, 1])
        self._an_frames += len(data)
        if self._an_frames < 4096:
            return
        x = np.concatenate(self._an_buf)[-4096:]
        self._an_buf, self._an_frames = [], 0
        if np.sqrt(np.mean(x ** 2)) < 0.003:  # silence: learn nothing
            return
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
        fr = np.fft.rfftfreq(len(x), 1 / SAMPLE_RATE)
        f = band_freqs(self.n)
        edges = np.sqrt(f[:-1] * f[1:])
        edges = np.concatenate([[f[0] / 1.4], edges, [f[-1] * 1.4]])
        bands = np.array([spec[(fr >= lo) & (fr < hi)].sum() + 1e-12 for lo, hi in zip(edges[:-1], edges[1:])])
        db = 10 * np.log10(bands)
        self._avg = db if self._avg is None or len(self._avg) != len(db) else 0.99 * self._avg + 0.01 * db
        now = time.monotonic()
        if now - self._last_smart < 2:
            return
        self._last_smart = now
        # Audiophile target: Harman-style in-room curve (gentle bass shelf, smooth
        # downward tilt). Relative to pink noise, which is how music energy is measured.
        target = np.array([curve_db([(20, 4), (60, 4), (200, 1), (1000, 0), (10000, -3), (20000, -5)], fq)
                           for fq in f])
        dev = (self._avg - self._avg.mean()) - (target - target.mean())
        # Fit only three broad, smooth controls - never narrow per-band fixes, which
        # would colour the sound: a low shelf, an overall tilt, and a presence bump.
        lf = np.log2(f / 1000.0)
        shelf = 1 / (1 + (f / 150.0) ** 2)  # 1 in the bass, 0 above ~300 Hz
        tilt = lf / np.max(np.abs(lf))
        pres = np.exp(-((lf - np.log2(3)) ** 2) / 0.8)  # broad bump around 3 kHz
        basis = np.column_stack([shelf, tilt, pres, np.ones_like(f)])
        coef, *_ = np.linalg.lstsq(basis, -dev, rcond=None)
        corr = basis[:, :3] @ np.clip(coef[:3], -3, 3)
        corr = np.clip(corr - corr.mean(), -3, 3)  # shape only, small: respect the master
        self.auto = 0.85 * self.auto + 0.15 * corr  # slow: settles over ~30 s, never pumps
        self.style = self._classify(fr, spec, dev, f)
        self._rebuild(keep_state=True)

    @staticmethod
    def _classify(fr, spec, dev, f):
        total = spec.sum() + 1e-12
        voice = spec[(fr > 300) & (fr < 3400)].sum() / total
        low = dev[f < 150].mean() if np.any(f < 150) else 0
        high = dev[f > 5000].mean() if np.any(f > 5000) else 0
        mid = dev[(f > 800) & (f < 4000)].mean() if np.any((f > 800) & (f < 4000)) else 0
        if voice > 0.75 and low < -3:
            return "Speech / podcast"
        if low > 4:
            return "Bass-heavy (hip-hop, EDM, engines)"
        if high > 3:
            return "Bright (pop, rock cymbals)"
        if mid > 3:
            return "Vocal-forward"
        if low < -4 and high < -2:
            return "Thin / mid-range"
        return "Balanced"


EQ = Equalizer()


# ------------------------------------------------------------ sim racing --

# Process names (lower-case) of racing sims. When one is running, the app switches
# to the Sim racing EQ and switches back when it closes.
SIM_PROCESSES = {
    "iracingsim64dx11.exe": "iRacing",
    "acc.exe": "Assetto Corsa Competizione",
    "acs.exe": "Assetto Corsa",
    "acs_x86.exe": "Assetto Corsa",
    "assettocorsaevo.exe": "Assetto Corsa EVO",
    "acevo.exe": "Assetto Corsa EVO",
    "rfactor2.exe": "rFactor 2",
    "le mans ultimate.exe": "Le Mans Ultimate",
    "lemansultimate.exe": "Le Mans Ultimate",
    "ams2avx.exe": "Automobilista 2",
    "ams2.exe": "Automobilista 2",
    "rrre64.exe": "RaceRoom",
    "rrre.exe": "RaceRoom",
    "beamng.drive.x64.exe": "BeamNG.drive",
    "beamng.x64.exe": "BeamNG.drive",
    "forzamotorsport.exe": "Forza Motorsport",
    "forzahorizon5.exe": "Forza Horizon 5",
    "forzahorizon6.exe": "Forza Horizon 6",
    "f1_24.exe": "F1 24",
    "f1_25.exe": "F1 25",
    "f1_26.exe": "F1 26",
    "wrc.exe": "EA Sports WRC",
    "dirtrally2.exe": "DiRT Rally 2.0",
    "pcars2avx.exe": "Project CARS 2",
    "pcars2.exe": "Project CARS 2",
    "richardburnsrally_sse.exe": "Richard Burns Rally",
    "gtr2.exe": "GTR 2",
    "rbr.exe": "Richard Burns Rally",
}


def running_sim():
    """Return the name of a running racing sim, or None (Windows only)."""
    if sys.platform != "win32":
        return None
    import subprocess

    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=0x08000000,  # no console window
        ).stdout.lower()
    except Exception:
        return None
    for line in out.splitlines():
        exe = line.split('","')[0].strip('"')
        if exe in SIM_PROCESSES:
            return SIM_PROCESSES[exe]
    return None


# ------------------------------------------------------------------ backend --


class Session:
    def __init__(self, name, channel):
        self.name = name
        self.channel = channel
        self.buf = LiveBuffer(channel=channel)
        self.atv = None
        self.playing = False


class Backend:
    """Runs pyatv on its own asyncio loop thread. One Session per speaker."""

    def __init__(self, log, on_status):
        self.log = log
        self.on_status = on_status
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run_loop, daemon=True).start()
        self.devices = {}
        self.sessions = {}
        self.capture = None
        self.gen = 0  # bumps on every Connect/Disconnect so old loops quit
        self.auto_fix = True
        self.on_delay_change = None  # UI callback when auto-fix raises the delay
        self._last_args = None
        self.source = ""

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _report(self):
        if not self.sessions:
            return
        playing = [s.name for s in self.sessions.values() if s.playing]
        waiting = [s.name for s in self.sessions.values() if not s.playing]
        parts = []
        if playing:
            parts.append("\U0001F512 Playing on " + " + ".join(playing))
        if waiting:
            parts.append("connecting " + " + ".join(waiting))
        self.on_status(" | ".join(parts))

    async def scan(self):
        self.on_status("Scanning for speakers...")
        try:
            found = await pyatv.scan(self.loop, timeout=5, protocol=Protocol.RAOP)
        except Exception as ex:
            self.log(f"Scan failed: {ex!r}")
            self.on_status("Scan failed - see log")
            return []
        self.devices = {}
        for conf in found:
            svc = conf.get_service(Protocol.RAOP)
            model = getattr(conf.device_info, "model_str", "") or ""
            osv = getattr(conf.device_info, "version", "") or ""
            self.devices[conf.name] = conf
            props = dict(svc.properties) if svc else {}
            self.log(
                f"Found: {conf.name} @ {conf.address} model={model} os={osv} "
                f"port={svc.port if svc else '?'} pairing={svc.pairing if svc else '?'} "
                f"features={props.get('ft') or props.get('features', '?')} "
                f"sf={props.get('sf', '?')} pw={props.get('pw', '?')}"
            )
        self.on_status(f"Found {len(found)} speaker(s)")
        return sorted(self.devices.keys())

    async def connect(self, names, channels, protocol_label, volume):
        """names: speakers to play on; channels: matching list of both/left/right."""
        await self.disconnect(quiet=True)
        self.gen += 1
        my_gen = self.gen
        self._last_args = (names, channels, protocol_label, volume)
        asyncio.ensure_future(self._health_monitor(my_gen))
        if self.capture is None or not self.capture.is_alive():
            self.capture = LoopbackCapture(self.log, self.source)
            self.capture.start()
        tasks = []
        for name, channel in zip(names, channels):
            if name not in self.devices:
                self.log(f"{name} not found - rescan")
                continue
            sess = Session(name, channel)
            self.sessions[name] = sess
            tasks.append(self._speaker_loop(sess, protocol_label, volume, my_gen))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _speaker_loop(self, sess, protocol_label, volume, my_gen):
        attempt = 0
        while my_gen == self.gen:
            attempt += 1
            try:
                await self._stream_once(sess, protocol_label, volume, attempt, my_gen)
            except asyncio.CancelledError:
                break
            except Exception as ex:
                if my_gen != self.gen:
                    return  # user disconnected or switched speakers
                self.log(f"{sess.name}: stream error: {ex!r}")
                self.log(traceback.format_exc().strip())
                cause = ex.__cause__ or ex.__context__
                if cause:
                    self.log(f"  caused by: {cause!r}")
            finally:
                sess.playing = False
                if self.capture:
                    self.capture.remove(sess.buf)
            if my_gen != self.gen:
                return
            if attempt >= 5 and not self.auto_fix:
                self.on_status(f"{sess.name} couldn't stay connected - copy the log and send it")
                break
            wait = min(15, 2 * attempt)
            self.on_status(f"{sess.name} dropped - reconnecting in {wait} s (try {attempt})...")
            await asyncio.sleep(wait)
        if my_gen == self.gen:
            self._close_atv(sess)

    async def _stream_once(self, sess, protocol_label, volume, attempt, my_gen):
        conf = self.devices[sess.name]
        storage = MemoryStorage()
        settings = await storage.get_settings(conf)
        # Always AirPlay 2: it's the only mode with encrypted audio.
        settings.protocols.raop.protocol_version = AirPlayVersion.V2
        self.log(
            f"Connect attempt {attempt}: {sess.name} ({sess.channel}), "
            f"protocol={protocol_label}, latency={LATENCY_MS['value']} ms"
        )
        self._report()
        sess.atv = await pyatv.connect(conf, self.loop, protocol=Protocol.RAOP, storage=storage)
        sess.buf.clear()
        sess.buf.dropped = sess.buf.underruns = sess.buf.captured = 0
        _live_sources[sess.name] = LiveSource(sess.buf)
        self.capture.add(sess.buf)
        try:
            await sess.atv.audio.set_volume(float(volume))
        except Exception as ex:
            self.log(f"{sess.name}: initial volume deferred: {ex!r}")
        started = time.monotonic()

        async def confirm():
            await asyncio.sleep(3)
            if my_gen == self.gen:
                sess.playing = True
                self._report()

        confirm_task = asyncio.ensure_future(confirm())
        try:
            await sess.atv.stream.stream_file(LIVE_SENTINEL + sess.name)
        finally:
            confirm_task.cancel()
        secs = max(0.1, time.monotonic() - started)
        self.log(
            f"{sess.name}: stream ended after {secs:.1f} s "
            f"(late reads={sess.buf.underruns}, resyncs={sess.buf.dropped}, "
            f"capture writer resyncs={TIMELINE.writer_resyncs}, "
            f"audio rate={sess.buf.captured / secs:.0f} Hz)"
        )
        if my_gen == self.gen:
            self._close_atv(sess)

    async def _health_monitor(self, my_gen):
        """Self-correction: watch for choppy audio and fix it automatically.

        - Speakers reporting lost audio (network) -> raise the delay and reconnect.
        - Our own reads arriving before the PC audio (capture hiccups) -> read a
          little further back, live, with no reconnect.
        """
        await asyncio.sleep(10)  # let things settle after connecting
        last_lost = dict(LOST)
        last_late = {}
        bad_windows = 0
        clean_windows = 0
        while my_gen == self.gen:
            await asyncio.sleep(10)
            if my_gen != self.gen or not self.auto_fix:
                last_lost = dict(LOST)
                continue
            worst = 0.0
            for sess in list(self.sessions.values()):
                ip = str(self.devices[sess.name].address) if sess.name in self.devices else ""
                lost = LOST.get(ip, 0) - last_lost.get(ip, 0)
                rate = lost / (10 * SAMPLE_RATE / 352)  # fraction of packets lost
                worst = max(worst, rate)
                if lost:
                    self.log(f"Auto-fix: {sess.name} lost {lost} packets in 10 s ({rate:.1%})")
                late = sess.buf.underruns - last_late.get(sess.name, 0)
                last_late[sess.name] = sess.buf.underruns
                if late > 3 and Timeline.READ_DELAY < 0.3:
                    Timeline.READ_DELAY = round(Timeline.READ_DELAY + 0.02, 3)
                    self.log(f"Auto-fix: PC audio arriving late - reading {Timeline.READ_DELAY * 1000:.0f} ms back")
            last_lost = dict(LOST)
            bad_windows = bad_windows + 1 if worst > 0.02 else 0
            clean_windows = clean_windows + 1 if worst == 0 else 0
            if clean_windows == 18 and LATENCY_MS["value"] > MIN_DELAY_MS and self.on_delay_change:
                # 3 clean minutes: try 25 ms less next time Play is pressed (no interruption now)
                lower = max(MIN_DELAY_MS, LATENCY_MS["value"] - 25)
                self.log(f"Auto-tune: 3 clean minutes - next Play will use {lower} ms")
                self.on_delay_change(lower, apply_now=False)
            if bad_windows >= 2 and LATENCY_MS["value"] < 2000 and self._last_args:
                new = min(2000, LATENCY_MS["value"] + 100)
                self.log(f"Auto-fix: sound was choppy for 20 s - raising delay to {new} ms and reconnecting")
                LATENCY_MS["value"] = new
                if self.on_delay_change:
                    self.on_delay_change(new)
                self.on_status(f"Fixing choppy sound - delay raised to {new} ms")
                asyncio.ensure_future(self.connect(*self._last_args))
                return

    def _close_atv(self, sess):
        if sess.atv:
            try:
                sess.atv.close()
            except Exception:
                pass
            sess.atv = None

    def set_source(self, name):
        self.source = name
        if self.capture and self.capture.is_alive():
            self.capture.set_source(name)

    async def set_volume(self, volume):
        for sess in list(self.sessions.values()):
            if sess.atv and sess.playing:
                try:
                    await sess.atv.audio.set_volume(float(volume))
                except Exception as ex:
                    self.log(f"{sess.name}: volume change failed: {ex!r}")

    async def disconnect(self, quiet=False):
        self.gen += 1
        sessions = list(self.sessions.values())
        self.sessions = {}
        for sess in sessions:
            if self.capture:
                self.capture.remove(sess.buf)
            if sess.atv:
                try:
                    await sess.atv.remote_control.stop()
                except Exception:
                    pass
            self._close_atv(sess)
        if not quiet:
            self.on_status("Disconnected")


# ------------------------------------------------------------------ updates --


LAST_RELEASE_SHA = {"value": None}


def fetch_latest_release():
    """Return (build_number, exe_url, release_name) of the newest GitHub release."""
    import urllib.request

    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "HomePodLink"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        rel = json.load(r)
    import re

    m = re.search(r"sha256:\s*([0-9a-fA-F]{64})", rel.get("body") or "")
    LAST_RELEASE_SHA["value"] = m.group(1).lower() if m else None
    tag = rel.get("tag_name", "")
    num = int(tag.split("-")[-1]) if tag.split("-")[-1].isdigit() else 0
    url = next(
        (a["browser_download_url"] for a in rel.get("assets", []) if a["name"].lower().endswith(".exe")),
        None,
    )
    return num, url, rel.get("name") or tag


def download_and_restart(url, log, progress=None):
    """Download the new exe next to this one, then swap it in and relaunch."""
    import subprocess
    import tempfile
    import urllib.request

    exe = sys.executable
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Updating only works from the .exe")
    new_path = exe + ".new"
    req = urllib.request.Request(url, headers={"User-Agent": "HomePodLink"})
    with urllib.request.urlopen(req, timeout=120) as r, open(new_path, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done * 100 // total)
    if os.path.getsize(new_path) < 1_000_000:
        os.remove(new_path)
        raise RuntimeError("Download looks incomplete")
    import hashlib

    h = hashlib.sha256()
    with open(new_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    expected = LAST_RELEASE_SHA["value"]
    if not expected or h.hexdigest() != expected:
        os.remove(new_path)
        raise RuntimeError("Update failed its security check (checksum mismatch) - not installed")
    log("Update checksum verified")
    log(f"Downloaded update ({os.path.getsize(new_path) // 1_000_000} MB), restarting")
    bat = os.path.join(tempfile.gettempdir(), "homepodlink_update.bat")
    with open(bat, "w") as f:
        f.write(
            "@echo off\r\n"
            "set N=0\r\n"
            ":retry\r\n"
            "ping -n 2 127.0.0.1 >nul\r\n"
            # The exe stays locked until both PyInstaller processes exit, so keep
            # trying the swap (up to ~60 s) instead of trying once.
            f'move /Y "{new_path}" "{exe}" >nul 2>&1\r\n'
            "if errorlevel 1 (set /a N+=1 & if %N% LSS 60 goto retry)\r\n"
            f'start "" "{exe}"\r\n'
            'del "%~f0"\r\n'
        )
    subprocess.Popen(
        ["cmd", "/c", bat],
        creationflags=0x08000000 | 0x00000008,  # CREATE_NO_WINDOW | DETACHED_PROCESS
        close_fds=True,
    )


# ---------------------------------------------------------------- reports --


class Reporter:
    """Sends problems to the GitHub repo as issues so they can be fixed without
    anyone copying logs. One issue per app session; later events become comments.
    Rate-limited so a bad night can't flood the repo."""

    MIN_GAP = 120  # seconds between posts
    MAX_POSTS = 15  # per app session

    def __init__(self, get_log_lines, log):
        self.get_log_lines = get_log_lines
        self.log = log
        self.enabled = True
        self.issue_number = None
        self.last_post = 0.0
        self.posts = 0
        self.pending = []
        self.lock = threading.Lock()

    @property
    def available(self):
        return bool(REPORT_TOKEN)

    def event(self, reason):
        """Queue a report. Safe to call from any thread."""
        if not (self.enabled and self.available):
            return
        with self.lock:
            self.pending.append(f"{time.strftime('%H:%M:%S')} {reason}")
        threading.Thread(target=self._flush, daemon=True).start()

    def _api(self, method, path, body):
        import urllib.request

        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}{path}",
            data=json.dumps(body).encode(),
            method=method,
            headers={
                "Authorization": f"Bearer {REPORT_TOKEN}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "HomePodLink",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)

    def _flush(self, force=False):
        with self.lock:
            if not self.pending:
                return
            if self.posts >= self.MAX_POSTS:
                self.pending.clear()
                return
            if not force and time.time() - self.last_post < self.MIN_GAP:
                # try again once the gap has passed
                threading.Timer(self.MIN_GAP, self._flush).start()
                return
            reasons, self.pending = self.pending, []
            self.last_post = time.time()
            self.posts += 1
        import re

        def scrub(t):
            t = re.sub(r"\b\d{1,3}(\.\d{1,3}){3}\b", "[ip]", t)
            t = re.sub(r"(?i)([A-Z]:\\Users\\)[^\\]+", r"\1[user]", t)
            return t

        lines = [scrub(x) for x in self.get_log_lines()[-150:]]
        reasons = [scrub(r) for r in reasons]
        body = (
            "**Why:**\n" + "\n".join(f"- {r}" for r in reasons)
            + f"\n\n**App:** {APP_NAME} {APP_VERSION} build {BUILD_NUMBER} on {platform.system()} {platform.release()}"
            + "\n\n<details><summary>Log (last 150 lines)</summary>\n\n```\n"
            + "\n".join(lines)[-60000:]
            + "\n```\n</details>"
        )
        try:
            if self.issue_number is None:
                res = self._api("POST", "/issues", {
                    "title": f"[auto] Build {BUILD_NUMBER}: {reasons[0][9:80]}",
                    "body": body,
                })
                self.issue_number = res.get("number")
                self.log(f"Report sent to GitHub (issue #{self.issue_number})")
            else:
                self._api("POST", f"/issues/{self.issue_number}/comments", {"body": body})
                self.log(f"Report added to GitHub issue #{self.issue_number}")
        except Exception as ex:
            self.log(f"Couldn't send report: {ex!r}")

    def send_now(self, note="Sent manually"):
        if not self.available:
            return False
        with self.lock:
            self.pending.append(f"{time.strftime('%H:%M:%S')} {note}")
        threading.Thread(target=self._flush, kwargs={"force": True}, daemon=True).start()
        return True


# ----------------------------------------------------------------------- UI --

RAINBOW = ["#E40303", "#FF8C00", "#FFED00", "#008026", "#004DFF", "#750787"]
VIZ_STYLES = ("Bars", "Mirror", "Wave", "Dots", "Scope")
VIZ_PALETTES = {
    "Theme": None,  # accent colour, or rainbow in the rainbow theme
    "Rainbow": RAINBOW,
    "Sunset": ["#FF5F6D", "#FF8A5B", "#FFC371", "#F857A6", "#B24592"],
    "Ocean": ["#00C6FB", "#0099F7", "#005BEA", "#2E3192"],
    "Neon": ["#39FF14", "#00FFF0", "#FF00E6", "#FFE600"],
    "Mono": ["#8E8E93"],
}
PURPLE = "#750787"

THEMES = {
    "plain": {
        "bg": "#F2F2F7", "card": "#FFFFFF", "text": "#1C1C1E", "sub": "#6C6C70",
        "sep": "#E5E5EA", "accent": "#007AFF", "accent_dn": "#0062CC", "on": "#34C759",
        "off": "#E5E5EA", "muted": "#8E8E93", "knob_border": "#D1D1D6",
    },
    "rainbow": {
        "bg": "#F7F2FA", "card": "#FFFFFF", "text": "#1C1C1E", "sub": "#6C6C70",
        "sep": "#EEE5F2", "accent": PURPLE, "accent_dn": "#5A055F", "on": "#008026",
        "off": "#E5E5EA", "muted": "#8E8E93", "knob_border": "#D1D1D6",
    },
}

FONT = "Segoe UI Variable Text" if sys.platform == "win32" else "Segoe UI"
FONT_DISPLAY = "Segoe UI Variable Display" if sys.platform == "win32" else "Segoe UI"


def _round_rect(cv, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


class Card(tk.Canvas):
    """White rounded card; put widgets in .inner."""

    def __init__(self, parent, th, radius=12):
        super().__init__(parent, bg=th["bg"], highlightthickness=0, bd=0, height=10)
        self.th, self.r = th, radius
        self.inner = tk.Frame(self, bg=th["card"])
        self._win = self.create_window(0, 0, window=self.inner, anchor="nw")
        self.bind("<Configure>", self._redraw)
        self.inner.bind("<Configure>", self._redraw)

    def _redraw(self, _e=None):
        w = self.winfo_width()
        h = self.inner.winfo_reqheight() + 8
        self.configure(height=h)
        self.delete("bgshape")
        _round_rect(self, 0, 0, w, h, self.r, fill=self.th["card"], outline="", tags="bgshape")
        self.tag_lower("bgshape")
        self.coords(self._win, 0, 4)
        self.itemconfigure(self._win, width=w)


class Toggle(tk.Canvas):
    """iOS-style on/off switch bound to a BooleanVar."""

    def __init__(self, parent, var, th, command=None):
        super().__init__(parent, width=46, height=28, bg=th["card"], highlightthickness=0, bd=0, cursor="hand2")
        self.var, self.th, self.command = var, th, command
        self.bind("<Button-1>", self._click)
        self._draw()

    def _click(self, _e):
        self.var.set(not self.var.get())
        self._draw()
        if self.command:
            self.command()

    def _draw(self):
        self.delete("all")
        on = self.var.get()
        _round_rect(self, 1, 1, 45, 27, 13, fill=self.th["on"] if on else self.th["off"], outline="")
        x = 31 if on else 15
        self.create_oval(x - 11, 3, x + 11, 25, fill="white", outline=self.th["knob_border"])


class Slider(tk.Canvas):
    """Thin Apple-style slider with a big white knob."""

    def __init__(self, parent, var, lo, hi, th, step=1, command=None, rainbow=False):
        super().__init__(parent, height=30, bg=th["card"], highlightthickness=0, bd=0, cursor="hand2")
        self.var, self.lo, self.hi, self.th = var, lo, hi, th
        self.step, self.command, self.rainbow = step, command, rainbow
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)

    def _x(self, v):
        w = self.winfo_width()
        return 14 + (w - 28) * (v - self.lo) / (self.hi - self.lo)

    def _drag(self, e):
        w = self.winfo_width()
        frac = min(1, max(0, (e.x - 14) / max(1, w - 28)))
        v = self.lo + frac * (self.hi - self.lo)
        v = int(round(v / self.step) * self.step)
        if v != self.var.get():
            self.var.set(v)
            self._draw()
            if self.command:
                self.command(v)

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        x = self._x(self.var.get())
        _round_rect(self, 14, 12, w - 14, 18, 3, fill=self.th["off"], outline="")
        if self.rainbow:
            seg = (x - 14) / len(RAINBOW)
            for i, c in enumerate(RAINBOW):
                self.create_rectangle(14 + i * seg, 12, 14 + (i + 1) * seg, 18, fill=c, outline=c)
        else:
            _round_rect(self, 14, 12, max(20, x), 18, 3, fill=self.th["accent"], outline="")
        self.create_oval(x - 12, 3, x + 12, 27, fill="white", outline=self.th["knob_border"])


class BigButton(tk.Canvas):
    def __init__(self, parent, text, th, command, rainbow=False):
        super().__init__(parent, height=46, bg=th["bg"], highlightthickness=0, bd=0, cursor="hand2")
        self.text, self.th, self.command, self.rainbow = text, th, command, rainbow
        self.pressed = False
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<ButtonPress-1>", lambda e: self._press(True))
        self.bind("<ButtonRelease-1>", self._release)

    def set_text(self, text):
        self.text = text
        self._draw()

    def _press(self, p):
        self.pressed = p
        self._draw()

    def _release(self, e):
        self._press(False)
        if 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self.command()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        fill = self.th["accent_dn"] if self.pressed else self.th["accent"]
        _round_rect(self, 0, 0, w, 46, 12, fill=fill, outline="")
        if self.rainbow:
            seg = (w - 24) / len(RAINBOW)
            for i, c in enumerate(RAINBOW):
                self.create_rectangle(12 + i * seg, 40, 12 + (i + 1) * seg, 43, fill=c, outline=c)
        self.create_text(w / 2, 22, text=self.text, fill="white", font=(FONT, 12, "bold"))


class App:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        LATENCY_MS["value"] = self.settings["latency_ms"]
        self.logq = queue.Queue()
        self.log_lines = []
        self._setup_file_log()

        self.vol = tk.IntVar(value=self.settings["volume"])
        self.lat = tk.IntVar(value=self.settings["latency_ms"])
        self.split = tk.BooleanVar(value=self.settings["stereo_split"])
        self.swap = tk.BooleanVar(value=self.settings["swap_lr"])
        self.rainbow = tk.BooleanVar(value=self.settings["rainbow"])
        self.auto_fix = tk.BooleanVar(value=self.settings.get("auto_fix", True))
        self.send_reports = tk.BooleanVar(value=self.settings.get("send_reports", True))
        self.proto = tk.StringVar(value="AirPlay 2")
        self.show_log = tk.BooleanVar(value=False)
        self.page = "main"
        self.eq_on = tk.BooleanVar(value=self.settings.get("eq_on", False))
        self.smart_eq = tk.BooleanVar(value=self.settings.get("smart_eq", False))
        EQ.set_bands(int(self.settings.get("eq_bands", 10)))
        if self.settings.get("eq_gains") and len(self.settings["eq_gains"]) == EQ.n:
            EQ.set_manual(self.settings["eq_gains"])
        elif self.settings.get("eq_preset") in PRESETS:
            EQ.apply_preset(self.settings["eq_preset"])
        EQ.enabled, EQ.smart = self.eq_on.get(), self.smart_eq.get()
        self._eq_save_job = None
        self.auto_sim = tk.BooleanVar(value=self.settings.get("auto_sim", True))
        self._sim_active = None  # name of the sim we switched for
        self._sim_saved = None  # EQ state to restore afterwards
        self.status = tk.StringVar(value="Looking for speakers...")
        self.state = "idle"  # idle | connecting | playing
        self.names = []
        self.selected = set(self.settings.get("last_devices", []))
        self._vol_job = None

        root.geometry("440x880")
        root.minsize(380, 560)
        self.body = None
        self.build()

        self.reporter = Reporter(lambda: list(self.log_lines), self.log)
        self.reporter.enabled = self.send_reports.get()
        self.backend = Backend(self.log, self.set_status)
        self.backend.auto_fix = self.auto_fix.get()
        if self.settings.get("source") is None:
            line1 = find_line1(list_sources())
            self.settings["source"] = line1
            save_settings(self.settings)
            if line1:
                self.root.after(500, self.build)
        self.backend.source = self.settings.get("source") or ""
        self.backend.on_delay_change = lambda v, apply_now=True: self.root.after(
            0, lambda: self._delay_changed(v, apply_now)
        )
        self.log(
            f"{APP_NAME} {APP_VERSION} build {BUILD_NUMBER} | {platform.platform()} | "
            f"Python {platform.python_version()} | pyatv {pyatv.const.__version__}"
        )
        self.root.after(100, self._drain_log)
        self.root.after(3000, self._sim_watch)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.on_scan()
        self.root.after(4000, lambda: self.on_update(quiet=True))

    # ------------------------------------------------------------- layout --
    def build(self):
        """(Re)build the whole window. Called again when the theme changes."""
        if self.body is not None:
            self.body.destroy()
        self.log_box = None
        rb = self.rainbow.get()
        th = self.th = THEMES["rainbow" if rb else "plain"]
        self.root.configure(bg=th["bg"])
        self.root.title(f"{APP_NAME} (build {BUILD_NUMBER})" + (" \U0001F308" if rb else ""))
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=th["card"], background=th["card"])

        body = self.body = tk.Frame(self.root, bg=th["bg"])
        body.pack(fill="both", expand=True)

        if rb:
            stripe = tk.Canvas(body, height=6, highlightthickness=0, bd=0)
            stripe.pack(fill="x")
            stripe.bind("<Configure>", lambda e: self._stripe(stripe))

        wrap = tk.Frame(body, bg=th["bg"])
        wrap.pack(fill="both", expand=True, padx=16, pady=(12, 12))

        def lbl(parent, text, size=11, color="text", bold=False, bg="card", **kw):
            return tk.Label(parent, text=text, bg=th[bg], fg=th[color],
                            font=(FONT, size, "bold" if bold else "normal"), **kw)

        # title + status
        tk.Label(wrap, text=APP_NAME, bg=th["bg"], fg=th["text"],
                 font=(FONT_DISPLAY, 20, "bold")).pack(anchor="w")
        srow = tk.Frame(wrap, bg=th["bg"])
        srow.pack(fill="x", pady=(0, 12))
        self.dot = tk.Canvas(srow, width=10, height=10, bg=th["bg"], highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 6))
        tk.Label(srow, textvariable=self.status, bg=th["bg"], fg=th["sub"], font=(FONT, 10),
                 anchor="w", justify="left", wraplength=360).pack(side="left", fill="x")
        self._update_dot()

        # segmented control: Speakers | Sound
        seg = tk.Frame(wrap, bg=th["off"])
        seg.pack(fill="x", pady=(0, 12))
        for key, text in (("main", "Speakers"), ("sound", "Sound")):
            sel = self.page == key
            b = tk.Label(seg, text=text, bg=th["card"] if sel else th["off"], fg=th["text"],
                         font=(FONT, 10, "bold" if sel else "normal"), pady=5, cursor="hand2")
            b.pack(side="left", fill="x", expand=True, padx=2, pady=2)
            b.bind("<Button-1>", lambda e, k=key: self._go(k))
        if self.page == "sound":
            self._build_sound(wrap, th, lbl, rb)
            return

        # speakers
        lbl(wrap, "SPEAKERS", 9, "sub", bg="bg").pack(anchor="w", padx=4, pady=(0, 4))
        self.spk_card = Card(wrap, th)
        self.spk_card.pack(fill="x", pady=(0, 14))
        self._render_speakers()

        # audio source
        lbl(wrap, "AUDIO SOURCE", 9, "sub", bg="bg").pack(anchor="w", padx=4, pady=(0, 4))
        src_card = Card(wrap, th)
        src_card.pack(fill="x", pady=(0, 14))
        srow2 = tk.Frame(src_card.inner, bg=th["card"], cursor="hand2")
        srow2.pack(fill="x", padx=12, pady=8)
        a = lbl(srow2, self._source_label(), 11)
        a.pack(side="left")
        b = lbl(srow2, "Change \u203a", 10, "accent")
        b.pack(side="right")
        for w in (srow2, a, b):
            w.bind("<Button-1>", lambda e, anchor=srow2: self._source_menu(anchor))

        # sliders
        sc = Card(wrap, th)
        sc.pack(fill="x", pady=(0, 14))
        vrow = tk.Frame(sc.inner, bg=th["card"])
        vrow.pack(fill="x", padx=12, pady=(6, 2))
        lbl(vrow, "Volume", 11).pack(side="left")
        self.vol_lbl = lbl(vrow, str(self.vol.get()), 11, bold=True)
        self.vol_lbl.pack(side="right")
        Slider(sc.inner, self.vol, 0, 100, th, command=self.on_volume, rainbow=rb).pack(fill="x", padx=4)
        lrow = tk.Frame(sc.inner, bg=th["card"])
        lrow.pack(fill="x", padx=12, pady=(8, 2))
        lbl(lrow, "Delay", 11).pack(side="left")
        self.lat_lbl = lbl(lrow, f"{self.lat.get()} ms", 11, bold=True)
        self.lat_lbl.pack(side="right")
        Slider(sc.inner, self.lat, MIN_DELAY_MS, 2000, th, step=25, command=self.on_latency, rainbow=rb).pack(fill="x", padx=4)
        lbl(sc.inner, "Lower = less lag. With Auto-fix on, the app finds the lowest smooth delay by itself.",
            9, "sub", wraplength=360, justify="left").pack(anchor="w", padx=12, pady=(2, 8))

        # toggles
        tc = Card(wrap, th)
        tc.pack(fill="x", pady=(0, 14))
        rows = [("Auto-fix choppy sound", self.auto_fix, self.on_auto_fix),
                ("Send problem reports to GitHub", self.send_reports, self.on_send_reports),
                ("Stereo pair (left + right)", self.split, self._render_speakers),
                ("Swap left and right", self.swap, self._render_speakers),
                ("Rainbow theme", self.rainbow, self.on_theme)]
        for i, (text, var, cmd) in enumerate(rows):
            r = tk.Frame(tc.inner, bg=th["card"])
            r.pack(fill="x", padx=12, pady=6)
            lbl(r, text, 11).pack(side="left")
            Toggle(r, var, th, command=cmd).pack(side="right")
            if i < len(rows) - 1:
                tk.Frame(tc.inner, bg=th["sep"], height=1).pack(fill="x", padx=(12, 0))

        # play / stop
        self.play_btn = BigButton(wrap, self._play_text(), th, self.on_play, rainbow=rb)
        self.play_btn.pack(fill="x", pady=(0, 10))

        # link row
        links = tk.Frame(wrap, bg=th["bg"])
        links.pack(pady=(0, 6))
        upd = f"Update to {self._update_available}" if getattr(self, "_update_available", None) else "Update"
        for text, cmd in (("Rescan", self.on_scan), (upd, self.on_update),
                          ("GitHub", self.open_repo), ("Copy log", self.copy_log),
                          ("Report", self.report_now)):
            l = tk.Label(links, text=text, bg=th["bg"], fg=th["accent"], font=(FONT, 10), cursor="hand2")
            l.pack(side="left", padx=8)
            l.bind("<Button-1>", lambda e, c=cmd: c())

        # advanced (mode + log), collapsed by default
        adv = tk.Label(wrap, text=("Hide advanced" if self.show_log.get() else "Advanced"),
                       bg=th["bg"], fg=th["sub"], font=(FONT, 9), cursor="hand2")
        adv.pack()
        adv.bind("<Button-1>", lambda e: (self.show_log.set(not self.show_log.get()), self.build()))
        if self.show_log.get():
            ac = Card(wrap, th)
            ac.pack(fill="both", expand=True, pady=(6, 0))
            mrow = tk.Frame(ac.inner, bg=th["card"])
            mrow.pack(fill="x", padx=12, pady=(6, 4))
            lbl(mrow, "Connection", 10).pack(side="left")
            lbl(mrow, "AirPlay 2, encrypted (ChaCha20-Poly1305)", 10, "sub").pack(side="right")
            self.log_box = tk.Text(ac.inner, height=9, wrap="word", font=("Consolas", 8),
                                   bg=th["card"], fg=th["text"], relief="flat", highlightthickness=0)
            self.log_box.pack(fill="both", expand=True, padx=10, pady=(0, 8))
            self.log_box.insert("end", "\n".join(self.log_lines[-300:]) + "\n")
            self.log_box.see("end")
        else:
            self.log_box = None

    def _go(self, page):
        self.page = page
        self.build()

    def _build_sound(self, wrap, th, lbl, rb):
        # visualizer
        lbl(wrap, "VISUALIZER", 9, "sub", bg="bg").pack(anchor="w", padx=4, pady=(0, 4))
        vc = Card(wrap, th)
        vc.pack(fill="x", pady=(0, 14))
        self.viz_h = int(self.settings.get("viz_height", 110))
        self.viz = tk.Canvas(vc.inner, height=self.viz_h, bg=th["card"], highlightthickness=0, cursor="hand2")
        self.viz.pack(fill="x", padx=12, pady=(8, 0))
        # drag handle: pull down/up to resize the visualizer
        grip = tk.Canvas(vc.inner, height=14, bg=th["card"], highlightthickness=0, cursor="sb_v_double_arrow")
        grip.pack(fill="x", padx=12, pady=(0, 4))
        grip.bind("<Configure>", lambda e: (grip.delete("all"), _round_rect(
            grip, e.width / 2 - 22, 5, e.width / 2 + 22, 10, 2, fill=th["knob_border"], outline="")))
        grip.bind("<ButtonPress-1>", lambda e: setattr(self, "_viz_drag", (e.y_root, self.viz_h)))
        grip.bind("<B1-Motion>", self._viz_resize)
        grip.bind("<ButtonRelease-1>", lambda e: (self.settings.__setitem__("viz_height", self.viz_h),
                                                  save_settings(self.settings), vc._redraw()))
        self.viz.bind("<Button-1>", lambda e: self._viz_set("viz_style", VIZ_STYLES[
            (VIZ_STYLES.index(self.settings.get("viz_style", "Bars")) + 1) % len(VIZ_STYLES)]))
        self._viz_levels = np.zeros(32)
        self._viz_peaks = np.zeros(32)

        def chips(parent, key, options, current):
            row = tk.Frame(parent, bg=th["card"])
            row.pack(fill="x", padx=10, pady=(0, 4))
            for o in options:
                sel = o == current
                b = tk.Label(row, text=o, bg=th["accent"] if sel else th["off"], fg="white" if sel else th["text"],
                             font=(FONT, 8), padx=6, pady=2, cursor="hand2")
                b.pack(side="left", padx=2, fill="x", expand=True)
                b.bind("<Button-1>", lambda e, o=o: self._viz_set(key, o))

        chips(vc.inner, "viz_style", VIZ_STYLES, self.settings.get("viz_style", "Bars"))
        chips(vc.inner, "viz_colors", list(VIZ_PALETTES), self.settings.get("viz_colors", "Theme"))
        self._viz_tick()

        # equalizer
        lbl(wrap, "EQUALIZER", 9, "sub", bg="bg").pack(anchor="w", padx=4, pady=(0, 4))
        ec = Card(wrap, th)
        ec.pack(fill="both", expand=True)
        for i, (text, var, cmd) in enumerate((("Equalizer", self.eq_on, self._eq_toggle),
                                              ("Smart EQ (audiophile, Harman target)", self.smart_eq, self._eq_toggle),
                                              ("Auto Sim racing when a sim starts", self.auto_sim, self._auto_sim_toggle))):
            r = tk.Frame(ec.inner, bg=th["card"])
            r.pack(fill="x", padx=12, pady=5)
            lbl(r, text, 11).pack(side="left")
            Toggle(r, var, th, command=cmd).pack(side="right")
        if self._sim_active:
            lbl(ec.inner, f"\U0001F3CE {self._sim_active} running - Sim racing sound on", 9, "accent").pack(anchor="w", padx=12)
        self.style_lbl = lbl(ec.inner, "", 9, "sub")
        self.style_lbl.pack(anchor="w", padx=12)
        tk.Frame(ec.inner, bg=th["sep"], height=1).pack(fill="x", padx=12, pady=6)

        # band count
        brow = tk.Frame(ec.inner, bg=th["card"])
        brow.pack(fill="x", padx=12, pady=(0, 6))
        lbl(brow, "Bands", 10).pack(side="left")
        for n in reversed(BAND_CHOICES):
            sel = EQ.n == n
            b = tk.Label(brow, text=str(n), bg=th["accent"] if sel else th["off"],
                         fg="white" if sel else th["text"], font=(FONT, 9, "bold"), padx=9, pady=2, cursor="hand2")
            b.pack(side="right", padx=2)
            b.bind("<Button-1>", lambda e, n=n: self._eq_bands(n))

        # presets
        prow = tk.Frame(ec.inner, bg=th["card"])
        prow.pack(fill="x", padx=10, pady=(0, 6))
        cur = self.settings.get("eq_preset", "Flat")
        for i, name in enumerate(PRESETS):
            sel = name == cur and not self.smart_eq.get()
            b = tk.Label(prow, text=name, bg=th["accent"] if sel else th["off"],
                         fg="white" if sel else th["text"], font=(FONT, 9), padx=8, pady=3, cursor="hand2")
            b.grid(row=i // 3, column=i % 3, padx=2, pady=2, sticky="ew")
            b.bind("<Button-1>", lambda e, n=name: self._eq_preset(n))
        for c in range(3):
            prow.columnconfigure(c, weight=1)

        # band sliders (vertical, drawn on one canvas so 32 bands still fit)
        self.eqc = tk.Canvas(ec.inner, height=170, bg=th["card"], highlightthickness=0, cursor="hand2")
        self.eqc.pack(fill="x", padx=8, pady=(4, 10))
        self.eqc.bind("<Configure>", lambda e: self._eq_draw())
        self.eqc.bind("<Button-1>", self._eq_drag)
        self.eqc.bind("<B1-Motion>", self._eq_drag)
        self.eqc.bind("<Double-Button-1>", self._eq_reset_band)
        self._eq_refresh()

    def _eq_geom(self):
        w = self.eqc.winfo_width()
        n = EQ.n
        left, right, top, bot = 26, 6, 8, 140
        step = (w - left - right) / n
        return left, step, top, bot

    def _eq_draw(self):
        cv, th = self.eqc, self.th
        cv.delete("all")
        left, step, top, bot = self._eq_geom()
        mid = (top + bot) / 2
        for db in (12, 0, -12):
            y = mid - db / 12 * (bot - top) / 2
            cv.create_line(left, y, left + step * EQ.n, y, fill=th["sep"])
            cv.create_text(left - 4, y, text=f"{db:+d}" if db else "0", anchor="e", fill=th["sub"], font=(FONT, 7))
        gains = EQ.gains
        live = self.eq_on.get()
        for i, (f, g) in enumerate(zip(EQ.freqs, gains)):
            x = left + step * (i + 0.5)
            y = mid - g / 12 * (bot - top) / 2
            col = (RAINBOW[i * len(RAINBOW) // EQ.n] if self.rainbow.get() else th["accent"]) if live else th["muted"]
            cv.create_line(x, mid, x, y, fill=col, width=max(2, min(6, step * 0.35)))
            r = max(4, min(8, step * 0.32))
            cv.create_oval(x - r, y - r, x + r, y + r, fill="white", outline=th["knob_border"])
            every = max(1, EQ.n // 8)
            if i % every == 0:
                lab = f"{f / 1000:.0f}k" if f >= 1000 else f"{f:.0f}"
                cv.create_text(x, bot + 14, text=lab, fill=th["sub"], font=(FONT, 7))

    def _eq_drag(self, e):
        left, step, top, bot = self._eq_geom()
        i = int((e.x - left) // step)
        if not 0 <= i < EQ.n:
            return
        g = (((top + bot) / 2) - e.y) / ((bot - top) / 2) * 12
        g = round(max(-12, min(12, g)) * 2) / 2
        if self.smart_eq.get():  # touching a slider takes manual control
            EQ.manual = EQ.auto.copy()
            self.smart_eq.set(False)
            EQ.smart = False
            self.settings["smart_eq"] = False
        gains = EQ.manual.copy()
        gains[i] = g
        EQ.set_manual(gains)
        self.settings["eq_preset"] = "Custom"
        self._eq_draw()
        self._eq_save_later()

    def _eq_reset_band(self, e):
        left, step, top, bot = self._eq_geom()
        i = int((e.x - left) // step)
        if 0 <= i < EQ.n and not self.smart_eq.get():
            gains = EQ.manual.copy()
            gains[i] = 0
            EQ.set_manual(gains)
            self._eq_draw()
            self._eq_save_later()

    def _eq_save_later(self):
        if self._eq_save_job:
            self.root.after_cancel(self._eq_save_job)

        def save():
            self.settings.update(eq_gains=[float(g) for g in EQ.manual], eq_bands=EQ.n,
                                 eq_on=self.eq_on.get(), smart_eq=self.smart_eq.get())
            save_settings(self.settings)

        self._eq_save_job = self.root.after(500, save)

    def _auto_sim_toggle(self):
        self.settings["auto_sim"] = self.auto_sim.get()
        save_settings(self.settings)
        if not self.auto_sim.get() and self._sim_active:
            self._sim_apply(None)

    def _eq_toggle(self):
        EQ.enabled = self.eq_on.get()
        EQ.smart = self.smart_eq.get()
        if EQ.smart and not EQ.enabled:  # Smart EQ needs the EQ on
            self.eq_on.set(True)
            EQ.enabled = True
        EQ._rebuild(keep_state=True)
        self._eq_save_later()
        self.build()

    def _eq_bands(self, n):
        EQ.set_bands(n)
        if self.settings.get("eq_preset") in PRESETS:
            EQ.apply_preset(self.settings["eq_preset"])
        self._eq_save_later()
        self.build()

    def _eq_preset(self, name):
        EQ.apply_preset(name)
        EQ.smart = False
        self.smart_eq.set(False)
        if not self.eq_on.get():
            self.eq_on.set(True)
            EQ.enabled = True
        self.settings["eq_preset"] = name
        self._eq_save_later()
        self.build()

    def _eq_refresh(self):
        """Keep the sliders and style label live while Smart EQ is steering."""
        if self.page != "sound":
            return
        try:
            if self.smart_eq.get():
                self._eq_draw()
                self.style_lbl.config(text=f"Sounds like: {EQ.style}" if EQ.style else "Listening...")
            else:
                self.style_lbl.config(text="")
        except tk.TclError:
            return
        self.root.after(1000, self._eq_refresh)

    def _viz_tick(self):
        if self.page != "sound":
            return
        try:
            cv = self.viz
            w = cv.winfo_width()
        except tk.TclError:
            return
        n = 2048
        with TIMELINE.lock:
            wp = TIMELINE.write_pos
            if wp is not None:
                a = (wp - n) % TIMELINE.size
                x = np.take(TIMELINE.ring[:, 0] + TIMELINE.ring[:, 1], range(a, a + n), mode="wrap")
            else:
                x = np.zeros(n)
        spec = np.abs(np.fft.rfft(x * np.hanning(n)))
        fr = np.fft.rfftfreq(n, 1 / SAMPLE_RATE)
        edges = np.geomspace(40, 16000, 33)
        lv = np.array([spec[(fr >= lo) & (fr < hi)].mean() if np.any((fr >= lo) & (fr < hi))
                       else np.interp(np.sqrt(lo * hi), fr, spec)  # bass bands narrower than an FFT bin
                       for lo, hi in zip(edges[:-1], edges[1:])])
        lv = np.clip((20 * np.log10(lv + 1e-9) + 10) / 50, 0, 1)
        self._viz_levels = np.maximum(lv, self._viz_levels * 0.85)  # fast attack, smooth fall
        self._viz_peaks = np.maximum(self._viz_levels, self._viz_peaks - 0.015)
        h = self.viz_h
        style = self.settings.get("viz_style", "Bars")
        pal = VIZ_PALETTES.get(self.settings.get("viz_colors", "Theme"))
        if pal is None:
            pal = RAINBOW if self.rainbow.get() else [self.th["accent"]]

        def col(i, n=32):
            return pal[min(len(pal) - 1, i * len(pal) // n)]

        cv.delete("all")
        bw = w / 32
        lv = self._viz_levels
        if style == "Bars":
            for i, v in enumerate(lv):
                bh = max(3, v * (h - 6))
                _round_rect(cv, i * bw + 2, h - bh, (i + 1) * bw - 2, h, 2, fill=col(i), outline="")
                py = h - max(3, self._viz_peaks[i] * (h - 6)) - 3
                cv.create_line(i * bw + 2, py, (i + 1) * bw - 2, py, fill=col(i), width=2)
        elif style == "Mirror":
            mid = h / 2
            for i, v in enumerate(lv):
                bh = max(2, v * (mid - 3))
                _round_rect(cv, i * bw + 2, mid - bh, (i + 1) * bw - 2, mid + bh, 2, fill=col(i), outline="")
        elif style == "Wave":
            pts = [(0, h)]
            for i, v in enumerate(lv):
                pts.append(((i + 0.5) * bw, h - 3 - v * (h - 8)))
            pts.append((w, h))
            flat = [c for p in pts for c in p]
            cv.create_polygon(flat, smooth=True, fill=col(16), outline="", stipple="gray50")
            cv.create_line(flat[2:-2], smooth=True, fill=col(4), width=3)
        elif style == "Dots":
            rows = max(4, int(h // 12))
            for i, v in enumerate(lv):
                lit = int(round(v * rows))
                for r in range(rows):
                    y = h - (r + 0.5) * (h / rows)
                    rr = min(bw, h / rows) * 0.32
                    cx = (i + 0.5) * bw
                    on = r < lit
                    cv.create_oval(cx - rr, y - rr, cx + rr, y + rr,
                                   fill=col(i) if on else self.th["off"], outline="")
        else:  # Scope: the actual waveform
            seg = x[-1024:]
            peak = max(0.05, float(np.max(np.abs(seg))))
            pts = []
            for j in range(0, 1024, 8):
                pts += [j / 1024 * w, h / 2 - seg[j] / peak * (h / 2 - 4)]
            cv.create_line(pts, fill=col(8), width=2, smooth=True)
        self.root.after(33, self._viz_tick)

    def _viz_resize(self, e):
        y0, h0 = getattr(self, "_viz_drag", (e.y_root, self.viz_h))
        self.viz_h = int(max(40, min(400, h0 + (e.y_root - y0))))
        self.viz.configure(height=self.viz_h)

    def _viz_set(self, key, value):
        self.settings[key] = value
        save_settings(self.settings)
        self.build()

    def _sim_watch(self):
        """Every 5 s: switch to the Sim racing EQ while a racing sim runs."""
        def check():
            name = running_sim() if self.auto_sim.get() else None
            self.root.after(0, lambda: self._sim_apply(name))

        threading.Thread(target=check, daemon=True).start()
        self.root.after(5000, self._sim_watch)

    def _sim_apply(self, name):
        if name and not self._sim_active:
            self._sim_saved = (EQ.enabled, EQ.smart, EQ.manual.copy(), self.settings.get("eq_preset"))
            self._sim_active = name
            EQ.smart = False
            EQ.enabled = True
            EQ.apply_preset("Sim racing")
            self.eq_on.set(True)
            self.smart_eq.set(False)
            self.settings["eq_preset"] = "Sim racing"
            self.set_status(f"{name} detected - Sim racing sound on")
            self.log(f"Sim racing mode: {name} started")
            self.build()
        elif not name and self._sim_active:
            self.log(f"Sim racing mode: {self._sim_active} closed - restoring your sound")
            enabled, smart, manual, preset = self._sim_saved
            EQ.set_manual(manual)
            EQ.enabled, EQ.smart = enabled, smart
            EQ._rebuild(keep_state=True)
            self.eq_on.set(enabled)
            self.smart_eq.set(smart)
            self.settings["eq_preset"] = preset
            self._sim_active = self._sim_saved = None
            self.set_status("Game closed - your normal sound is back")
            self.build()

    def _stripe(self, cv):
        cv.delete("all")
        w = cv.winfo_width()
        seg = w / len(RAINBOW)
        for i, c in enumerate(RAINBOW):
            cv.create_rectangle(i * seg, 0, (i + 1) * seg + 1, 6, fill=c, outline=c)

    def _update_dot(self):
        col = {"playing": self.th["on"], "connecting": "#FF9F0A"}.get(self.state, self.th["muted"])
        try:
            self.dot.delete("all")
            self.dot.create_oval(1, 1, 9, 9, fill=col, outline=col)
        except tk.TclError:
            pass

    def _play_text(self):
        return "Stop" if self.state in ("playing", "connecting") else "Play"

    def _sides(self):
        """Which side each selected speaker plays, when stereo pair is on."""
        sel = [n for n in self.names if n in self.selected]
        if self.split.get() and len(sel) == 2:
            sides = ["Left", "Right"]
            if self.swap.get():
                sides.reverse()
            return dict(zip(sel, sides))
        return {}

    def _render_speakers(self):
        if self.page != "main":
            return
        inner = self.spk_card.inner
        th = self.th
        for w in inner.winfo_children():
            w.destroy()
        if not self.names:
            tk.Label(inner, text="No speakers found yet - tap Rescan", bg=th["card"], fg=th["sub"],
                     font=(FONT, 10)).pack(anchor="w", padx=12, pady=10)
            return
        sides = self._sides()
        for i, n in enumerate(self.names):
            conf = getattr(self, "backend", None) and self.backend.devices.get(n)
            model = (getattr(conf.device_info, "model_str", "") or "") if conf else ""
            is_pod = "HomePod" in model
            on = n in self.selected
            row = tk.Frame(inner, bg=th["card"], cursor="hand2")
            row.pack(fill="x", padx=12, pady=7)
            icon = "\U0001F50A" if is_pod else "\U0001F4FA"
            parts = [tk.Label(row, text=icon, bg=th["card"], font=(FONT, 12)),
                     tk.Label(row, text=n, bg=th["card"], fg=th["text"] if (on or is_pod) else th["muted"],
                              font=(FONT, 11))]
            parts[0].pack(side="left", padx=(0, 8))
            parts[1].pack(side="left")
            if on:
                parts.append(tk.Label(row, text="✓", bg=th["card"], fg=th["accent"], font=(FONT, 13, "bold")))
                parts[-1].pack(side="right")
            if n in sides:
                parts.append(tk.Label(row, text=sides[n], bg=th["card"], fg=th["sub"], font=(FONT, 9)))
                parts[-1].pack(side="right", padx=8)
            for w in [row] + parts:
                w.bind("<Button-1>", lambda e, name=n: self._toggle_speaker(name))
            if i < len(self.names) - 1:
                tk.Frame(inner, bg=th["sep"], height=1).pack(fill="x", padx=(40, 0))

    def _toggle_speaker(self, name):
        if name in self.selected:
            self.selected.discard(name)
        else:
            self.selected.add(name)
        self._render_speakers()

    def on_auto_fix(self):
        self.backend.auto_fix = self.auto_fix.get()
        if self.settings.get("source") is None:
            line1 = find_line1(list_sources())
            self.settings["source"] = line1
            save_settings(self.settings)
            if line1:
                self.root.after(500, self.build)
        self.backend.source = self.settings.get("source") or ""
        self.settings["auto_fix"] = self.auto_fix.get()
        save_settings(self.settings)

    def _source_label(self):
        src = self.settings.get("source") or ""
        if not src:
            return "Everything the PC plays"
        return src if len(src) <= 38 else src[:36] + "\u2026"

    def _source_menu(self, anchor):
        sources = list_sources()
        m = tk.Menu(self.root, tearoff=0, font=(FONT, 10))
        m.add_command(label="Everything the PC plays (default output)", command=lambda: self._set_source(""))
        if sources:
            m.add_separator()
        for label in sources:
            m.add_command(label=label, command=lambda n=label: self._set_source(n))
        m.tk_popup(anchor.winfo_rootx(), anchor.winfo_rooty() + anchor.winfo_height())

    def _set_source(self, name):
        self.settings["source"] = name
        save_settings(self.settings)
        self.backend.set_source(name)
        self.log(f"Audio source set to: {name or 'default output'}")
        self.build()

    def on_send_reports(self):
        self.reporter.enabled = self.send_reports.get()
        self.settings["send_reports"] = self.send_reports.get()
        save_settings(self.settings)
        if self.send_reports.get() and not self.reporter.available:
            self.set_status("Reports aren't set up in this build yet")

    def report_now(self):
        if self.reporter.send_now():
            self.set_status("Sending report to GitHub...")
        else:
            self.set_status("Reports aren't set up in this build yet - use Copy log")

    def _delay_changed(self, v, apply_now=True):
        self.settings["latency_ms"] = v
        save_settings(self.settings)
        if apply_now:
            self.lat.set(v)
            self.build()
        else:
            self._pending_delay = v  # used on next Play; doesn't interrupt the stream

    # -------------------------------------------------------------- theme --
    def on_theme(self):
        self.settings["rainbow"] = self.rainbow.get()
        save_settings(self.settings)
        self.build()

    # ------------------------------------------------------------ logging --
    def _setup_file_log(self):
        try:
            os.makedirs(SETTINGS_DIR, exist_ok=True)
            logging.basicConfig(
                filename=LOG_FILE,
                filemode="w",
                level=logging.DEBUG,
                format="%(asctime)s %(name)s %(levelname)s %(message)s",
            )
        except Exception:
            pass

        class _UIHandler(logging.Handler):
            def emit(h, record):
                if record.levelno >= logging.WARNING:
                    self.log(f"[{record.name}] {record.getMessage()}")

        logging.getLogger("pyatv").addHandler(_UIHandler())

    REPORT_TRIGGERS = ("stream error", "Auto-fix:", "couldn't stay connected", "Capture error",
                       "Update failed", "Scan failed", "dropped - reconnecting")

    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        logging.getLogger("homepod_link").info(msg)
        self.logq.put(line)
        self.log_lines.append(line)
        reporter = getattr(self, "reporter", None)
        if reporter and any(t in msg for t in self.REPORT_TRIGGERS) and not msg.startswith("Status:"):
            reporter.event(msg[:200])

    def _drain_log(self):
        while not self.logq.empty():
            line = self.logq.get()
            if self.log_box is not None:
                try:
                    self.log_box.insert("end", line + "\n")
                    self.log_box.see("end")
                except tk.TclError:
                    pass
        self.root.after(150, self._drain_log)

    def set_status(self, text):
        def apply():
            self.status.set(text)
            low = text.lower()
            if low.lstrip("\U0001F512 ").startswith("playing"):
                self.state = "playing"
            elif "connecting" in low or "pairing" in low or "reconnecting" in low:
                self.state = "connecting"
            elif low.startswith("disconnected") or "couldn't stay connected" in low:
                self.state = "idle"
            self._update_dot()
            try:
                self.play_btn.set_text(self._play_text())
            except tk.TclError:
                pass

        self.root.after(0, apply)
        self.log(f"Status: {text}")

    def copy_log(self):
        text = "\n".join(self.log_lines) + f"\n\n(full debug log: {LOG_FILE})"
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status("Log copied - paste it to Claude")

    # ------------------------------------------------------------ actions --
    def on_scan(self):
        fut = self.backend.submit(self.backend.scan())

        def done(f):
            names = f.result() if not f.exception() else []
            self.root.after(0, lambda: self._fill_devices(names))

        fut.add_done_callback(done)

    def _maybe_resume(self):
        if self.settings.get("resume_after_update"):
            self.settings["resume_after_update"] = False
            save_settings(self.settings)
            self.log(f"Updated to build {BUILD_NUMBER} - resuming playback")
            self.on_connect()

    def _fill_devices(self, names):
        def is_pod(n):
            return "HomePod" in (getattr(self.backend.devices[n].device_info, "model_str", "") or "")

        names = sorted(names, key=lambda n: (not is_pod(n), n))
        self.names = names
        keep = {n for n in self.selected if n in names}
        if not keep:
            keep = {n for n in names if "HomePod" in (getattr(self.backend.devices[n].device_info, "model_str", "") or "")}
        if not keep and names:
            keep = {names[0]}
        self.selected = keep
        self._render_speakers()
        self._maybe_resume()

    def on_play(self):
        if self.state in ("playing", "connecting"):
            self.state = "idle"
            self.play_btn.set_text("Play")
            self._update_dot()
            self.backend.submit(self.backend.disconnect())
        else:
            self.on_connect()

    def on_connect(self):
        names = [n for n in self.names if n in self.selected]
        if not names:
            self.set_status("Tap at least one speaker")
            return
        if self.split.get() and len(names) == 2:
            channels = ["left", "right"]
            if self.swap.get():
                channels.reverse()
        else:
            if self.split.get():
                self.log("Stereo pair needs exactly 2 speakers - playing full stereo on each")
            channels = ["both"] * len(names)
        if getattr(self, "_pending_delay", None) is not None:
            self.lat.set(self._pending_delay)
            self._pending_delay = None
            self.build()
        LATENCY_MS["value"] = self.lat.get()
        Timeline.READ_DELAY = 0.05
        self.settings.update(
            last_devices=names,
            latency_ms=self.lat.get(),
            protocol=self.proto.get(),
            volume=self.vol.get(),
            stereo_split=self.split.get(),
            swap_lr=self.swap.get(),
        )
        save_settings(self.settings)
        self.state = "connecting"
        self.play_btn.set_text("Stop")
        self._update_dot()
        self.backend.submit(self.backend.connect(names, channels, self.proto.get(), self.vol.get()))

    def on_volume(self, v):
        self.vol_lbl.config(text=str(v))
        if self._vol_job:
            self.root.after_cancel(self._vol_job)
        self._vol_job = self.root.after(200, lambda: self._apply_volume(v))

    def _apply_volume(self, v):
        self.settings["volume"] = v
        save_settings(self.settings)
        self.backend.submit(self.backend.set_volume(v))

    def on_latency(self, v):
        self.lat_lbl.config(text=f"{v} ms")

    def open_repo(self):
        import webbrowser

        webbrowser.open(f"https://github.com/{REPO}/releases/latest")

    def on_update(self, quiet=False):
        if not quiet:
            self.set_status("Checking for updates...")

        def work():
            try:
                num, url, name = fetch_latest_release()
                self.log(f"Latest release: {name} (this is build {BUILD_NUMBER})")
                if num <= BUILD_NUMBER or not url:
                    if not quiet:
                        self.set_status(f"You're up to date (build {BUILD_NUMBER})")
                    return
                if quiet:  # startup check: offer it, don't interrupt
                    self.root.after(0, lambda: self._offer_update(num))
                    return
                last = [-1]

                def prog(pct):
                    if pct // 5 != last[0]:
                        last[0] = pct // 5
                        self.root.after(0, lambda: self.status.set(f"Downloading build {num}... {pct}%"))

                download_and_restart(url, self.log, prog)
                # Come back playing on the same speakers after the restart.
                self.settings["resume_after_update"] = self.state in ("playing", "connecting")
                self.root.after(0, lambda: self.status.set(f"Installing build {num} - back in a few seconds"))
                self.root.after(600, self.on_close)
            except Exception as ex:
                self.log(f"Update failed: {ex!r}")
                if not quiet:
                    self.set_status("Update failed - see log")

        threading.Thread(target=work, daemon=True).start()

    def _offer_update(self, num):
        self._update_available = num
        self.set_status(f"Build {num} is available - tap Update")
        self.build()

    def on_close(self):
        pending = getattr(self, "_pending_delay", None)
        self.settings.update(
            latency_ms=pending if pending is not None else self.lat.get(),
            protocol=self.proto.get(),
            volume=self.vol.get(),
            stereo_split=self.split.get(),
            swap_lr=self.swap.get(),
            rainbow=self.rainbow.get(),
            auto_fix=self.auto_fix.get(),
            send_reports=self.send_reports.get(),
            eq_on=(self._sim_saved[0] if self._sim_saved else self.eq_on.get()),
            smart_eq=(self._sim_saved[1] if self._sim_saved else self.smart_eq.get()),
            auto_sim=self.auto_sim.get(),
            eq_bands=EQ.n,
            eq_gains=[float(g) for g in (self._sim_saved[2] if self._sim_saved else EQ.manual)],
            last_devices=[n for n in self.names if n in self.selected] or self.settings.get("last_devices", []),
        )
        save_settings(self.settings)
        try:
            self.backend.submit(self.backend.disconnect(quiet=True)).result(timeout=3)
        except Exception:
            pass
        self.root.destroy()


def main():
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
