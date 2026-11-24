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

## Sound tab

- **Visualizer:** live spectrum of what's being sent.
- **Equalizer:** 5, 10, 16 or 32 bands, adding zero delay. Presets: Flat, Bass boost, Vocal, Loudness, Night and **Sim racing** (engine and kerb rumble up, boxy low-mids down, tyre scrub and spotter calls up). Drag a band to adjust it, double-click to reset it.
- **Smart EQ:** a small analyser that runs on the PC, listens to the music, identifies its balance (bass-heavy, bright, vocal-forward, speech, balanced) and gently steers the EQ toward a balanced sound. Nothing leaves the PC.

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
