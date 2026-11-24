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
4. **Volume** controls the HomePod.
5. **Delay** sets the buffer: lower means less lag, but go too low and sound cuts out. Start at 500 ms and work down. Changes apply on the next Connect.
6. **Rainbow theme** switches between the rainbow look and the plain Windows look. It's remembered between launches.
7. **Mode** is AirPlay 2 by default. Auto and AirPlay 1 are there for troubleshooting.

**Check for update** (next to Copy log) downloads the newest build from GitHub and restarts the app on it.

If it won't connect, hit **Copy log** and send the log.

The full debug log is saved at `%APPDATA%\HomePodLink\homepod_link.log`.

## Self-correcting

With **Auto-fix choppy sound** on (default):
- If a HomePod reports lost audio for 20 seconds, the app raises the delay by 250 ms and reconnects.
- If PC audio arrives late, it reads a little further back with no interruption.
- If a speaker drops, it keeps reconnecting on its own.

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
