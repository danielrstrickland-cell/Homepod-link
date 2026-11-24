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
APP_VERSION = "0.1.1"
SAMPLE_RATE = 44100
CHANNELS = 2
BYTES_PER_SAMPLE = 2
FRAME_BYTES = CHANNELS * BYTES_PER_SAMPLE

SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "HomePodLink")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")
LOG_FILE = os.path.join(SETTINGS_DIR, "homepod_link.log")

DEFAULTS = {"latency_ms": 500, "volume": 50, "protocol": "AirPlay 2", "last_device": ""}

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

    def __init__(self, max_ms=120):
        self._lock = threading.Lock()
        self._buf = bytearray()
        self.max_bytes = int(SAMPLE_RATE * max_ms / 1000) * FRAME_BYTES
        self.dropped = 0
        self.underruns = 0

    def push(self, data: bytes):
        with self._lock:
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


LIVE_SENTINEL = "__homepod_link_live__"
_orig_open_source = raop_mod.open_source
_live_holder = {"source": None}


async def _patched_open_source(source, sample_rate, channels, sample_size):
    if isinstance(source, str) and source == LIVE_SENTINEL:
        return _live_holder["source"]
    return await _orig_open_source(source, sample_rate, channels, sample_size)


raop_mod.open_source = _patched_open_source

# ------------------------------------------------------------------ capture --


class LoopbackCapture(threading.Thread):
    """Records the default Windows output device via WASAPI loopback."""

    def __init__(self, buf: LiveBuffer, log):
        super().__init__(daemon=True)
        self.buf = buf
        self.log = log
        self._stop_evt = threading.Event()
        self.device_name = "?"
        self.error = None

    def stop(self):
        self._stop_evt.set()

    def run(self):
        try:
            import soundcard as sc  # imported here so errors land in the log
        except Exception as ex:
            self.error = f"Audio capture library failed to load: {ex}"
            self.log(self.error)
            return
        while not self._stop_evt.is_set():
            try:
                speaker = sc.default_speaker()
                self.device_name = speaker.name
                mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
                self.log(f"Capturing from: {speaker.name}")
                block = 441  # 10 ms blocks keep capture latency low
                with mic.recorder(samplerate=SAMPLE_RATE, channels=CHANNELS, blocksize=block) as rec:
                    while not self._stop_evt.is_set():
                        data = rec.record(numframes=block)
                        if data.ndim == 1:
                            data = np.column_stack([data, data])
                        elif data.shape[1] == 1:
                            data = np.repeat(data, 2, axis=1)
                        elif data.shape[1] > 2:
                            data = data[:, :2]
                        pcm = (np.clip(data, -1.0, 1.0) * 32767).astype("<i2").tobytes()
                        self.buf.push(pcm)
                        # Restart if the user switched default output device.
                        if sc.default_speaker().name != self.device_name:
                            self.log("Default output changed, restarting capture")
                            break
            except Exception as ex:
                self.log(f"Capture error: {ex!r} - retrying in 2 s")
                time.sleep(2)


# ------------------------------------------------------------------ backend --


class Backend:
    """Runs pyatv on its own asyncio loop thread."""

    def __init__(self, log, on_status):
        self.log = log
        self.on_status = on_status
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run_loop, daemon=True).start()
        self.devices = {}
        self.atv = None
        self.stream_task = None
        self.capture = None
        self.buf = LiveBuffer()
        self.connected_name = None
        self.want_connected = False
        self.gen = 0  # bumps on every Connect/Disconnect so old retry loops quit

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

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

    async def connect(self, name, protocol_label, volume):
        await self.disconnect(quiet=True)
        conf = self.devices.get(name)
        if conf is None:
            self.on_status("Speaker not found - rescan")
            return
        self.gen += 1
        my_gen = self.gen
        self.want_connected = True
        attempt = 0
        while self.want_connected and my_gen == self.gen:
            attempt += 1
            try:
                await self._stream_once(conf, name, protocol_label, volume, attempt)
            except asyncio.CancelledError:
                break
            except Exception as ex:
                self.log(f"Stream error: {ex!r}")
                self.log(traceback.format_exc().strip())
                cause = ex.__cause__ or ex.__context__
                if cause:
                    self.log(f"  caused by: {cause!r}")
            if not self.want_connected or my_gen != self.gen:
                return
            if attempt >= 5:
                self.on_status("Couldn't stay connected - copy the log and send it")
                self.want_connected = False
                break
            self.on_status(f"Dropped - reconnecting ({attempt}/5)...")
            await asyncio.sleep(2)
        await self._cleanup()

    async def _stream_once(self, conf, name, protocol_label, volume, attempt):
        self.on_status(f"Connecting to {name}...")
        storage = MemoryStorage()
        settings = await storage.get_settings(conf)
        settings.protocols.raop.protocol_version = {
            "AirPlay 2": AirPlayVersion.V2,
            "AirPlay 1": AirPlayVersion.V1,
        }.get(protocol_label, AirPlayVersion.Auto)
        self.log(
            f"Connect attempt {attempt}: {name}, protocol={protocol_label}, "
            f"latency={LATENCY_MS['value']} ms"
        )
        self.atv = await pyatv.connect(conf, self.loop, protocol=Protocol.RAOP, storage=storage)
        self.buf.clear()
        _live_holder["source"] = LiveSource(self.buf)
        if self.capture is None or not self.capture.is_alive():
            self.capture = LoopbackCapture(self.buf, self.log)
            self.capture.start()
        try:
            await self.atv.audio.set_volume(float(volume))
        except Exception as ex:
            self.log(f"Initial volume deferred: {ex!r}")
        self.connected_name = name
        self.on_status(f"Pairing with {name}...")
        started = time.monotonic()
        my_gen = self.gen

        async def confirm():
            await asyncio.sleep(3)
            if self.connected_name == name and my_gen == self.gen:
                self.on_status(f"Playing PC audio on {name}")

        confirm_task = asyncio.ensure_future(confirm())
        try:
            await self.atv.stream.stream_file(LIVE_SENTINEL)
        finally:
            confirm_task.cancel()
        self.log(
            f"Stream ended after {time.monotonic() - started:.1f} s "
            f"(underruns={self.buf.underruns}, dropped frames={self.buf.dropped})"
        )
        if my_gen == self.gen:
            self.connected_name = None
            if self.atv:
                self.atv.close()
                self.atv = None

    async def set_volume(self, volume):
        if self.atv and self.connected_name:
            try:
                await self.atv.audio.set_volume(float(volume))
            except Exception as ex:
                self.log(f"Volume change failed: {ex!r}")

    async def disconnect(self, quiet=False):
        self.want_connected = False
        self.gen += 1
        if self.atv:
            try:
                await self.atv.remote_control.stop()
            except Exception:
                pass
        await self._cleanup()
        if not quiet:
            self.on_status("Disconnected")

    async def _cleanup(self):
        if self.atv:
            try:
                self.atv.close()
            except Exception:
                pass
            self.atv = None
        self.connected_name = None


# ----------------------------------------------------------------------- UI --


class App:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        LATENCY_MS["value"] = self.settings["latency_ms"]
        self.logq = queue.Queue()
        self.log_lines = []
        self._setup_file_log()

        root.title(f"{APP_NAME} {APP_VERSION}")
        root.geometry("420x560")
        root.minsize(380, 480)

        pad = {"padx": 12, "pady": 4}
        ttk.Label(root, text="Speakers on your network").pack(anchor="w", **pad)
        self.dev_list = tk.Listbox(root, height=5, activestyle="none", exportselection=False)
        self.dev_list.pack(fill="x", padx=12)

        row = ttk.Frame(root)
        row.pack(fill="x", **pad)
        self.connect_btn = ttk.Button(row, text="Connect", command=self.on_connect)
        self.connect_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        ttk.Button(row, text="Disconnect", command=self.on_disconnect).pack(
            side="left", expand=True, fill="x", padx=4
        )
        ttk.Button(row, text="Rescan", command=self.on_scan).pack(
            side="left", expand=True, fill="x", padx=(4, 0)
        )

        # Volume
        vrow = ttk.Frame(root)
        vrow.pack(fill="x", **pad)
        ttk.Label(vrow, text="Volume", width=9).pack(side="left")
        self.vol = tk.IntVar(value=self.settings["volume"])
        self.vol_lbl = ttk.Label(vrow, text=str(self.vol.get()), width=5)
        ttk.Scale(vrow, from_=0, to=100, variable=self.vol, command=self.on_volume_drag).pack(
            side="left", fill="x", expand=True
        )
        self.vol_lbl.pack(side="left")
        self._vol_job = None

        # Delay
        lrow = ttk.Frame(root)
        lrow.pack(fill="x", **pad)
        ttk.Label(lrow, text="Delay (ms)", width=9).pack(side="left")
        self.lat = tk.IntVar(value=self.settings["latency_ms"])
        self.lat_lbl = ttk.Label(lrow, text=str(self.lat.get()), width=5)
        ttk.Scale(lrow, from_=250, to=2000, variable=self.lat, command=self.on_latency_drag).pack(
            side="left", fill="x", expand=True
        )
        self.lat_lbl.pack(side="left")
        ttk.Label(
            root,
            text="Lower = less lag. If sound cuts out, raise it. Applies on next Connect.",
            foreground="gray",
            wraplength=390,
        ).pack(anchor="w", padx=12)

        # Protocol
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

        self.status = tk.StringVar(value="Starting...")
        ttk.Label(root, textvariable=self.status, wraplength=390, font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=12, pady=(10, 4)
        )

        # Log
        lhead = ttk.Frame(root)
        lhead.pack(fill="x", padx=12)
        ttk.Label(lhead, text="Log").pack(side="left")
        ttk.Button(lhead, text="Copy log", command=self.copy_log).pack(side="right")
        self.log_box = tk.Text(root, height=10, wrap="word", font=("Consolas", 8))
        self.log_box.pack(fill="both", expand=True, padx=12, pady=(4, 12))

        self.backend = Backend(self.log, self.set_status)
        self.log(f"{APP_NAME} {APP_VERSION} | {platform.platform()} | Python {platform.python_version()} | pyatv {pyatv.const.__version__}")
        self.root.after(100, self._drain_log)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.on_scan()

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
        if names:
            idx = names.index(self.settings["last_device"]) if self.settings["last_device"] in names else 0
            self.dev_list.selection_set(idx)

    def on_connect(self):
        sel = self.dev_list.curselection()
        if not sel:
            self.set_status("Pick a speaker first")
            return
        name = self.dev_list.get(sel[0])
        LATENCY_MS["value"] = self.lat.get()
        self.settings.update(last_device=name, latency_ms=self.lat.get(), protocol=self.proto.get(), volume=self.vol.get())
        save_settings(self.settings)
        self.backend.submit(self.backend.connect(name, self.proto.get(), self.vol.get()))

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
        self.settings.update(latency_ms=self.lat.get(), protocol=self.proto.get(), volume=self.vol.get())
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
