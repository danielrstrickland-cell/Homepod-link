# HomePod Link

Play your Windows PC's sound through a HomePod over AirPlay 2.

Built because TuneBlade (AirPlay 1) stopped connecting to an original HomePod after a HomePod software update.

## Download

Grab `HomePodLink.exe` from the latest release on the **Releases** page. No install needed. Just run it.

Windows may show "Windows protected your PC" because the app isn't signed. Click **More info**, then **Run anyway**.
The first time it runs, allow it through the Windows Firewall on **Private networks**.

## Use

1. Make sure the PC and HomePod are on the same Wi-Fi, and Windows has that network set to **Private**.
2. Open HomePod Link. It scans for speakers automatically.
3. Tap one or more speakers (all HomePods are selected by default) and hit **Connect**. Everything the PC plays now comes out of them.
   - **Stereo pair (left + right):** with exactly 2 speakers picked, one plays the left channel and the other the right. **Swap L/R** if they're backwards.
   - Without it, each speaker plays full stereo.
4. **Audio source** sets what gets sent. Leave it on *Everything the PC plays*, or pick a single device such as Virtual Audio Cable's **Line 1 (input)**. Then only apps you route to Line 1 go to the HomePods. Line 1 is picked automatically the first time if it's installed.
5. **Volume** controls the HomePod.
5. **Delay** sets the buffer: lower means less lag, but go too low and sound cuts out. Start at 500 ms and work down. Changes apply on the next Connect.
6. **Rainbow theme** switches between the rainbow look and the plain Windows look. It's remembered between launches.
7. **Mode** is AirPlay 2 by default. Auto and AirPlay 1 are there for troubleshooting.

**Check for update** (next to Copy log) downloads the newest build from GitHub and restarts the app on it.

If it won't connect, hit **Copy log** and send the log.

The full debug log is saved at `%APPDATA%\HomePodLink\homepod_link.log`.

## 3D visualizer

GPU-rendered (OpenGL 3.3) in its own window, as a separate process, so it can never disturb the audio:
- **Modes:** 3D Waterfall, 3D Bars, 3D Ring and **Neon Highway** (drive between walls of light built from the music).
- **Look:** glossy lit bars, floor reflections, fog, HDR bloom, beat-reactive glow and camera, and sparks or speed streaks on kicks.
- **Controls:** drag to spin, scroll to zoom, double-click to reset, keys 1-4 switch modes, F for full screen.
- **Colors:** adds Nightclub, Turbo and Hyperdrive.
- **Accurate, not decorative:** a calibrated multi-resolution analyzer (16k/8k/4k/1k FFT, Hann, ENBW-corrected, full-scale sine = 0 dBFS, 64 log bands). It's delayed by the stream latency, so the picture matches what the HomePods are playing.
- **Pauses itself while a racing sim runs** (switchable), so it never costs frames on track.

## Audio engine

- **1 ms Windows timer:** smooth packet pacing instead of 15.6 ms bursts.
- **Pro Audio scheduling:** capture and network threads use Windows MMCSS "Pro Audio", and the process runs at high priority.
- **Studio resampling:** captures at 48 kHz and converts with soxr VHQ (artifacts below -100 dB) to AirPlay's 44.1 kHz.
- **TPDF dither:** used on the conversion to 16-bit; true silence stays silent.
- **Stream clock fixes:** the clock is re-anchored after setup, sync packets carry the real clock, and audio is selected per packet by its exact play time.
- **Stream health panel:** shows lost packets, late reads, resyncs, delay and capture rate.

## Sound tab

- **Visualizer:** live view of what's being sent. Styles: Bars (with peak caps), Mirror, Wave, Dots and Scope (the real waveform). Colors: Theme, Rainbow, Sunset, Ocean, Neon and Mono. Drag the handle under it to resize. Tap the visualizer to cycle styles.
- **Equalizer:** 5, 10, 16 or 32 bands, adding zero delay. Presets: Flat, Bass boost, Vocal, Loudness, Night and **Sim racing** (engine and kerb rumble up, boxy low-mids down, tyre scrub and spotter calls up). Drag a band to adjust it, double-click to reset it.
- **Auto Sim racing:** when a racing sim starts (iRacing, ACC, AC/EVO, rFactor 2, LMU, AMS2, RaceRoom, BeamNG, Forza, F1, WRC, DiRT Rally, Project CARS, RBR), the EQ switches to Sim racing. It switches back to your normal sound when the game closes.
- **Smart EQ (audiophile):** listens on the PC (nothing leaves it) and compares the long-term tonal balance to a Harman-style in-room target. It corrects only with three broad, smooth moves (bass shelf, tilt and presence), at most ±3 dB, settling over about 30 seconds, so it respects the master and never pumps. It also shows what it hears (bass-heavy, bright, vocal-forward, speech, balanced).

## Self-correcting

With **Auto-fix choppy sound** on (default):
- If a HomePod reports lost audio for 20 seconds, the app raises the delay by 250 ms and reconnects.
- If PC audio arrives late, it reads a little further back with no interruption.
- If a speaker drops, it keeps reconnecting on its own.

## Security

- **Encrypted audio:** always AirPlay 2. Every audio packet is encrypted with ChaCha20-Poly1305, using a key from a fresh X25519 pairing each session. The app refuses to send audio if encryption isn't active. AirPlay 1, which sends plain audio, is disabled.
- **Verified updates:** each release publishes the SHA-256 of its exe, and the app won't install an update that doesn't match.
- **Private reports:** IP addresses and the Windows user name are removed before a report is sent. Reports go over HTTPS with an issues-only key.
- **On your side:** keep the Wi-Fi on WPA2 or WPA3 with a strong password. Anyone you let onto the network can see the speakers, the same as with any AirPlay device.

## HomePod settings that matter

In the Home app: **Home Settings → Speakers & TV → Allow Speaker & TV Access** set to **Everyone** or **Anyone On the Same Network**, and **Require Password** off.

## How it works

- Captures the default Windows output with WASAPI loopback (`soundcard`)
- Streams real-time PCM to the HomePod using `pyatv` (AirPlay 2 with HAP transient pairing)
- Keeps the capture buffer short and pads silence so the stream stays up when the PC goes quiet

## Build it yourself

GitHub Actions builds the exe on every push to `main` (see `.github/workflows/build.yml`).
Locally on Windows:

```
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --windowed --name HomePodLink --collect-all pyatv --collect-all soundcard --collect-all miniaudio homepod_link.py
```
