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
APP_VERSION = "0.2.0"
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


class LiveBuffer:
    """Thread-safe PCM buffer between the capture thread and the sender."""

    def __init__(self, max_ms=200, channel="both"):
        self.channel = channel  # "both", "left" or "right"
        self._lock = threading.Lock()
        self._buf = bytearray()
        self.max_bytes = int(SAMPLE_RATE * max_ms / 1000) * FRAME_BYTES
        self.dropped = 0
        self.underruns = 0
        self.captured = 0

    def push(self, data: bytes):
        with self._lock:
            self.captured += len(data) // FRAME_BYTES
            self._buf.extend(data)
            # Keep latency bounded: if capture runs ahead (clock drift), drop oldest.
            extra = len(self._buf) - self.max_bytes
            if extra > 0:
                extra -= extra % FRAME_BYTES
                del self._buf[:extra]
                self.dropped += extra // FRAME_BYTES

    def pop(self, nbytes: int) -> bytes:
        with self._lock:
            if len(self._buf) >= nbytes:
                out = bytes(self._buf[:nbytes])
                del self._buf[:nbytes]
                return out
            # Not enough audio (silence on the PC or capture hiccup): send what we
            # have padded with silence so the stream never stops.
            out = bytes(self._buf) + b"\x00" * (nbytes - len(self._buf))
            if self._buf:
                self.underruns += 1
            self._buf.clear()
            return out

    def clear(self):
        with self._lock:
            self._buf.clear()


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
        with self._lock:
            bufs = list(self.buffers)
        if not bufs:
            return
        cache = {}
        for buf in bufs:
            if buf.channel not in cache:
                if buf.channel == "left":
                    d = np.column_stack([data[:, 0], data[:, 0]])
                elif buf.channel == "right":
                    d = np.column_stack([data[:, 1], data[:, 1]])
                else:
                    d = data
                # AirPlay wants big-endian 16-bit PCM
                cache[buf.channel] = (np.clip(d, -1.0, 1.0) * 32767).astype(">i2").tobytes()
            buf.push(cache[buf.channel])

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
            if attempt >= 5:
                self.on_status(f"{sess.name} couldn't stay connected - copy the log and send it")
                break
            self.on_status(f"{sess.name} dropped - reconnecting ({attempt}/5)...")
            await asyncio.sleep(2)
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
            f"(underruns={sess.buf.underruns}, dropped frames={sess.buf.dropped}, "
            f"capture rate={sess.buf.captured / secs:.0f} Hz)"
        )
        if my_gen == self.gen:
            self._close_atv(sess)

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


# ----------------------------------------------------------------------- UI --

RAINBOW = ["#E40303", "#FF8C00", "#FFED00", "#008026", "#004DFF", "#750787"]
PURPLE = "#750787"


class App:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        LATENCY_MS["value"] = self.settings["latency_ms"]
        self.logq = queue.Queue()
        self.log_lines = []
        self._setup_file_log()

        self.style = ttk.Style(root)
        self.default_theme = self.style.theme_use()

        root.geometry("440x640")
        root.minsize(400, 540)

        self.top_stripe = tk.Canvas(root, height=0, highlightthickness=0, bd=0)
        self.top_stripe.pack(fill="x")

        pad = {"padx": 12, "pady": 4}
        ttk.Label(root, text="Speakers on your network (tap one or more)").pack(anchor="w", **pad)
        self.dev_list = tk.Listbox(
            root, height=5, activestyle="none", exportselection=False, selectmode="multiple"
        )
        self.dev_list.pack(fill="x", padx=12)

        srow = ttk.Frame(root)
        srow.pack(fill="x", **pad)
        self.split = tk.BooleanVar(value=self.settings["stereo_split"])
        self.swap = tk.BooleanVar(value=self.settings["swap_lr"])
        ttk.Checkbutton(srow, text="Stereo pair (left + right)", variable=self.split).pack(side="left")
        ttk.Checkbutton(srow, text="Swap L/R", variable=self.swap).pack(side="left", padx=(12, 0))

        row = ttk.Frame(root)
        row.pack(fill="x", **pad)
        self.btn_connect = ttk.Button(row, text="Connect", command=self.on_connect)
        self.btn_connect.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.btn_disconnect = ttk.Button(row, text="Disconnect", command=self.on_disconnect)
        self.btn_disconnect.pack(side="left", expand=True, fill="x", padx=4)
        self.btn_rescan = ttk.Button(row, text="Rescan", command=self.on_scan)
        self.btn_rescan.pack(side="left", expand=True, fill="x", padx=(4, 0))

        vrow = ttk.Frame(root)
        vrow.pack(fill="x", **pad)
        ttk.Label(vrow, text="Volume", width=9, font=("Segoe UI", 10, "bold")).pack(side="left")
        self.vol = tk.IntVar(value=self.settings["volume"])
        self.vol_lbl = ttk.Label(vrow, text=str(self.vol.get()), width=5, style="Value.TLabel")
        ttk.Scale(vrow, from_=0, to=100, variable=self.vol, command=self.on_volume_drag, style="Big.Horizontal.TScale").pack(
            side="left", fill="x", expand=True, pady=6
        )
        self.vol_lbl.pack(side="left")
        self._vol_job = None

        lrow = ttk.Frame(root)
        lrow.pack(fill="x", **pad)
        ttk.Label(lrow, text="Delay (ms)", width=9, font=("Segoe UI", 10, "bold")).pack(side="left")
        self.lat = tk.IntVar(value=self.settings["latency_ms"])
        self.lat_lbl = ttk.Label(lrow, text=str(self.lat.get()), width=5, style="Value.TLabel")
        ttk.Scale(lrow, from_=250, to=2000, variable=self.lat, command=self.on_latency_drag, style="Big.Horizontal.TScale").pack(
            side="left", fill="x", expand=True, pady=6
        )
        self.lat_lbl.pack(side="left")
        ttk.Label(
            root,
            text="Lower = less lag. If sound cuts out, raise it. Applies on next Connect.",
            style="Hint.TLabel",
            wraplength=400,
        ).pack(anchor="w", padx=12)

        prow = ttk.Frame(root)
        prow.pack(fill="x", **pad)
        ttk.Label(prow, text="Mode", width=9).pack(side="left")
        self.proto = tk.StringVar(value=self.settings["protocol"])
        ttk.Combobox(
            prow,
            textvariable=self.proto,
            state="readonly",
            values=["AirPlay 2", "Auto", "AirPlay 1"],
            width=12,
        ).pack(side="left")
        self.rainbow = tk.BooleanVar(value=self.settings["rainbow"])
        ttk.Checkbutton(prow, text="Rainbow theme", variable=self.rainbow, command=self.apply_theme).pack(
            side="right"
        )

        self.status = tk.StringVar(value="Starting...")
        ttk.Label(root, textvariable=self.status, wraplength=400, style="Status.TLabel").pack(
            anchor="w", padx=12, pady=(10, 4)
        )

        lhead = ttk.Frame(root)
        lhead.pack(fill="x", padx=12)
        ttk.Label(lhead, text="Log").pack(side="left")
        ttk.Button(lhead, text="Copy log", command=self.copy_log).pack(side="right")
        self.log_box = tk.Text(root, height=10, wrap="word", font=("Consolas", 8))
        self.log_box.pack(fill="both", expand=True, padx=12, pady=(4, 8))

        self.bottom_stripe = tk.Canvas(root, height=0, highlightthickness=0, bd=0)
        self.bottom_stripe.pack(fill="x", side="bottom", before=lhead)
        for c in (self.top_stripe, self.bottom_stripe):
            c.bind("<Configure>", lambda e, c=c: self._draw_stripe(c))

        self.apply_theme()

        self.backend = Backend(self.log, self.set_status)
        self.log(
            f"{APP_NAME} {APP_VERSION} | {platform.platform()} | "
            f"Python {platform.python_version()} | pyatv {pyatv.const.__version__}"
        )
        self.root.after(100, self._drain_log)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.on_scan()

    # theme
    def _draw_stripe(self, canvas):
        canvas.delete("all")
        if not self.rainbow.get():
            return
        w = max(1, canvas.winfo_width())
        h = int(canvas.cget("height"))
        step = w / len(RAINBOW)
        for i, col in enumerate(RAINBOW):
            canvas.create_rectangle(i * step, 0, (i + 1) * step + 1, h, fill=col, outline=col)

    def apply_theme(self):
        on = self.rainbow.get()
        s = self.style
        if on:
            s.theme_use("clam")
            s.configure(".", font=("Segoe UI", 9))
            for name, col in (("Red", RAINBOW[0]), ("Blue", RAINBOW[4]), ("Green", RAINBOW[3])):
                s.configure(f"{name}.TButton", background=col, foreground="white", bordercolor=col)
                s.map(f"{name}.TButton", background=[("active", PURPLE)])
            s.configure("Big.Horizontal.TScale", troughcolor="#E9D5F0", background=PURPLE,
                        sliderthickness=24, sliderlength=28, troughrelief="flat", borderwidth=0)
            s.map("Big.Horizontal.TScale", background=[("active", RAINBOW[0])])
            s.configure("TCheckbutton", indicatorcolor="white")
            s.map("TCheckbutton", indicatorcolor=[("selected", PURPLE)])
            s.configure("Status.TLabel", foreground=PURPLE, font=("Segoe UI", 10, "bold"))
            s.configure("Hint.TLabel", foreground="gray")
            self.btn_connect.configure(style="Red.TButton")
            self.btn_disconnect.configure(style="Blue.TButton")
            self.btn_rescan.configure(style="Green.TButton")
            self.dev_list.configure(selectbackground=PURPLE, selectforeground="white")
            self.root.title(f"{APP_NAME} {APP_VERSION} \U0001F308")
            height = 8
        else:
            s.theme_use("clam")
            s.configure("Big.Horizontal.TScale", troughcolor="#DADADA", background="#0078D7",
                        sliderthickness=24, sliderlength=28, troughrelief="flat", borderwidth=0)
            s.map("Big.Horizontal.TScale", background=[("active", "#005A9E")])
            s.map("TCheckbutton", indicatorcolor=[("selected", "#0078D7")])
            s.configure("Status.TLabel", foreground="black", font=("Segoe UI", 10, "bold"))
            s.configure("Hint.TLabel", foreground="gray")
            for b in (self.btn_connect, self.btn_disconnect, self.btn_rescan):
                b.configure(style="TButton")
            self.dev_list.configure(selectbackground="#0078D7", selectforeground="white")
            self.root.title(f"{APP_NAME} {APP_VERSION}")
            height = 0
        s.configure("Value.TLabel", font=("Segoe UI", 11, "bold"))
        for c in (self.top_stripe, self.bottom_stripe):
            c.configure(height=height)
            self._draw_stripe(c)
        if self.settings.get("rainbow") != on:
            self.settings["rainbow"] = on
            save_settings(self.settings)

    # logging
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
            self.log_box.insert("end", line + "\n")
            self.log_box.see("end")
        self.root.after(150, self._drain_log)

    def set_status(self, text):
        self.root.after(0, lambda: self.status.set(text))
        self.log(f"Status: {text}")

    def copy_log(self):
        text = "\n".join(self.log_lines) + f"\n\n(full debug log: {LOG_FILE})"
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status("Log copied - paste it to Claude")

    # actions
    def on_scan(self):
        fut = self.backend.submit(self.backend.scan())

        def done(f):
            names = f.result() if not f.exception() else []
            self.root.after(0, lambda: self._fill_devices(names))

        fut.add_done_callback(done)

    def _fill_devices(self, names):
        self.dev_list.delete(0, "end")
        for n in names:
            self.dev_list.insert("end", n)
        remembered = [n for n in self.settings.get("last_devices", []) if n in names]
        homepods = [n for n in names if self.backend.devices.get(n) is not None
                    and "HomePod" in (getattr(self.backend.devices[n].device_info, "model_str", "") or "")]
        for n in remembered or homepods or names[:1]:
            self.dev_list.selection_set(names.index(n))

    def on_connect(self):
        names = [self.dev_list.get(i) for i in self.dev_list.curselection()]
        if not names:
            self.set_status("Pick at least one speaker")
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
        self.settings.update(
            last_devices=names,
            latency_ms=self.lat.get(),
            protocol=self.proto.get(),
            volume=self.vol.get(),
            stereo_split=self.split.get(),
            swap_lr=self.swap.get(),
        )
        save_settings(self.settings)
        self.backend.submit(self.backend.connect(names, channels, self.proto.get(), self.vol.get()))

    def on_disconnect(self):
        self.backend.submit(self.backend.disconnect())

    def on_volume_drag(self, _v):
        v = int(float(self.vol.get()))
        self.vol_lbl.config(text=str(v))
        if self._vol_job:
            self.root.after_cancel(self._vol_job)
        self._vol_job = self.root.after(200, lambda: self._apply_volume(v))

    def _apply_volume(self, v):
        self.settings["volume"] = v
        save_settings(self.settings)
        self.backend.submit(self.backend.set_volume(v))

    def on_latency_drag(self, _v):
        v = int(round(float(self.lat.get()) / 50) * 50)
        self.lat.set(v)
        self.lat_lbl.config(text=str(v))

    def on_close(self):
        self.settings.update(
            latency_ms=self.lat.get(),
            protocol=self.proto.get(),
            volume=self.vol.get(),
            stereo_split=self.split.get(),
            swap_lr=self.swap.get(),
            rainbow=self.rainbow.get(),
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
