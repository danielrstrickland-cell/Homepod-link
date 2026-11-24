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
APP_VERSION = "0.5.1"
BUILD_NUMBER = 0  # stamped by the GitHub build
REPO = "danielrstrickland-cell/Homepod-link"
SAMPLE_RATE = 44100
CHANNELS = 2
BYTES_PER_SAMPLE = 2
FRAME_BYTES = CHANNELS * BYTES_PER_SAMPLE

SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "HomePodLink")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")
LOG_FILE = os.path.join(SETTINGS_DIR, "homepod_link.log")

DEFAULTS = {
    "latency_ms": 500,
    "volume": 50,
    "protocol": "AirPlay 2",
    "last_devices": [],
    "stereo_split": False,
    "swap_lr": False,
    "rainbow": True,
    "auto_fix": True,
}

# ---------------------------------------------------------------- settings --


def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {**DEFAULTS, **data}
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
_orig_reset = StreamContext.reset


def _patched_reset(self):
    _orig_reset(self)
    ms = max(250, min(2000, int(LATENCY_MS["value"])))
    self.latency = int(self.sample_rate * ms / 1000)


StreamContext.reset = _patched_reset

# ------------------------------------------------------------- audio source --


class Timeline:
    """One shared, time-stamped ring of captured PC audio.

    Every speaker reads the slice that matches the wall clock (minus a small
    safety delay), so all speakers play the same moment at the same time no
    matter when each one connected."""

    SECONDS = 8
    READ_DELAY = 0.12  # read audio captured ~120 ms ago, so it has surely arrived
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
    """Records the default Windows output via WASAPI loopback and fans the
    audio out to every connected speaker's buffer."""

    def __init__(self, log):
        super().__init__(daemon=True)
        self.log = log
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self.buffers = []
        self.device_name = "?"

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
        TIMELINE.push(data.astype(np.float32), time.monotonic())

    def run(self):
        try:
            import soundcard as sc  # imported here so errors land in the log
        except Exception as ex:
            self.log(f"Audio capture library failed to load: {ex}")
            return
        while not self._stop_evt.is_set():
            try:
                speaker = sc.default_speaker()
                self.device_name = speaker.name
                mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
                self.log(f"Capturing from: {speaker.name}")
                block = 441  # 10 ms blocks keep capture latency low
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
                        if checks >= 100:  # about once a second
                            checks = 0
                            if sc.default_speaker().name != self.device_name:
                                self.log("Default output changed, restarting capture")
                                break
            except Exception as ex:
                self.log(f"Capture error: {ex!r} - retrying in 2 s")
                time.sleep(2)


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
            parts.append("Playing on " + " + ".join(playing))
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
            self.capture = LoopbackCapture(self.log)
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
        settings.protocols.raop.protocol_version = {
            "AirPlay 2": AirPlayVersion.V2,
            "AirPlay 1": AirPlayVersion.V1,
        }.get(protocol_label, AirPlayVersion.Auto)
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
                    Timeline.READ_DELAY = round(Timeline.READ_DELAY + 0.03, 3)
                    self.log(f"Auto-fix: PC audio arriving late - reading {Timeline.READ_DELAY * 1000:.0f} ms back")
            last_lost = dict(LOST)
            bad_windows = bad_windows + 1 if worst > 0.02 else 0
            if bad_windows >= 2 and LATENCY_MS["value"] < 2000 and self._last_args:
                new = min(2000, LATENCY_MS["value"] + 250)
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


def fetch_latest_release():
    """Return (build_number, exe_url, release_name) of the newest GitHub release."""
    import urllib.request

    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "HomePodLink"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        rel = json.load(r)
    tag = rel.get("tag_name", "")
    num = int(tag.split("-")[-1]) if tag.split("-")[-1].isdigit() else 0
    url = next(
        (a["browser_download_url"] for a in rel.get("assets", []) if a["name"].lower().endswith(".exe")),
        None,
    )
    return num, url, rel.get("name") or tag


def download_and_restart(url, log):
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
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    if os.path.getsize(new_path) < 1_000_000:
        raise RuntimeError("Download looks incomplete")
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


# ----------------------------------------------------------------------- UI --

RAINBOW = ["#E40303", "#FF8C00", "#FFED00", "#008026", "#004DFF", "#750787"]
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
        self.proto = tk.StringVar(value=self.settings["protocol"])
        self.show_log = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Looking for speakers...")
        self.state = "idle"  # idle | connecting | playing
        self.names = []
        self.selected = set(self.settings.get("last_devices", []))
        self._vol_job = None

        root.geometry("420x780")
        root.minsize(380, 560)
        self.body = None
        self.build()

        self.backend = Backend(self.log, self.set_status)
        self.backend.auto_fix = self.auto_fix.get()
        self.backend.on_delay_change = lambda v: self.root.after(0, lambda: self._delay_changed(v))
        self.log(
            f"{APP_NAME} {APP_VERSION} build {BUILD_NUMBER} | {platform.platform()} | "
            f"Python {platform.python_version()} | pyatv {pyatv.const.__version__}"
        )
        self.root.after(100, self._drain_log)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.on_scan()

    # ------------------------------------------------------------- layout --
    def build(self):
        """(Re)build the whole window. Called again when the theme changes."""
        if self.body is not None:
            self.body.destroy()
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

        # speakers
        lbl(wrap, "SPEAKERS", 9, "sub", bg="bg").pack(anchor="w", padx=4, pady=(0, 4))
        self.spk_card = Card(wrap, th)
        self.spk_card.pack(fill="x", pady=(0, 14))
        self._render_speakers()

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
        Slider(sc.inner, self.lat, 250, 2000, th, step=50, command=self.on_latency, rainbow=rb).pack(fill="x", padx=4)
        lbl(sc.inner, "Lower = less lag. Raise it if sound cuts out. Applies when you press Play.",
            9, "sub", wraplength=360, justify="left").pack(anchor="w", padx=12, pady=(2, 8))

        # toggles
        tc = Card(wrap, th)
        tc.pack(fill="x", pady=(0, 14))
        rows = [("Auto-fix choppy sound", self.auto_fix, self.on_auto_fix),
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
        for text, cmd in (("Rescan", self.on_scan), ("Check for update", self.on_update),
                          ("GitHub page", self.open_repo), ("Copy log", self.copy_log)):
            l = tk.Label(links, text=text, bg=th["bg"], fg=th["accent"], font=(FONT, 10), cursor="hand2")
            l.pack(side="left", padx=7)
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
            lbl(mrow, "Mode", 10).pack(side="left")
            ttk.Combobox(mrow, textvariable=self.proto, state="readonly",
                         values=["AirPlay 2", "Auto", "AirPlay 1"], width=11).pack(side="right")
            self.log_box = tk.Text(ac.inner, height=9, wrap="word", font=("Consolas", 8),
                                   bg=th["card"], fg=th["text"], relief="flat", highlightthickness=0)
            self.log_box.pack(fill="both", expand=True, padx=10, pady=(0, 8))
            self.log_box.insert("end", "\n".join(self.log_lines[-300:]) + "\n")
            self.log_box.see("end")
        else:
            self.log_box = None

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
        self.settings["auto_fix"] = self.auto_fix.get()
        save_settings(self.settings)

    def _delay_changed(self, v):
        self.lat.set(v)
        self.settings["latency_ms"] = v
        save_settings(self.settings)
        self.build()

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

    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        logging.getLogger("homepod_link").info(msg)
        self.logq.put(line)

    def _drain_log(self):
        while not self.logq.empty():
            line = self.logq.get()
            self.log_lines.append(line)
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
            if low.startswith("playing"):
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
        LATENCY_MS["value"] = self.lat.get()
        Timeline.READ_DELAY = 0.12
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

    def on_update(self):
        self.set_status("Checking for updates...")

        def work():
            try:
                num, url, name = fetch_latest_release()
                self.log(f"Latest release: {name} (this is build {BUILD_NUMBER})")
                if num <= BUILD_NUMBER or not url:
                    self.set_status(f"You're up to date (build {BUILD_NUMBER})")
                    return
                self.set_status(f"Downloading build {num}...")
                download_and_restart(url, self.log)
                self.root.after(0, self.on_close)
            except Exception as ex:
                self.log(f"Update failed: {ex!r}")
                self.set_status("Update failed - see log")

        threading.Thread(target=work, daemon=True).start()

    def on_close(self):
        self.settings.update(
            latency_ms=self.lat.get(),
            protocol=self.proto.get(),
            volume=self.vol.get(),
            stereo_split=self.split.get(),
            swap_lr=self.swap.get(),
            rainbow=self.rainbow.get(),
            auto_fix=self.auto_fix.get(),
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
