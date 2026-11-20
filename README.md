# RealTran — live Chinese / Japanese / English meeting subtitles

> **日本語:** [README.jp.md](README.jp.md)

Speech is transcribed live and every sentence is translated into the other two languages within
about 1–3 seconds. Everything runs on one PC with an NVIDIA GPU in your own network: no cloud
service, audio and text never leave the LAN.

| Step | Model | Developer | License |
|---|---|---|---|
| Speech recognition (final text) | Whisper large-v3 | OpenAI | Apache 2.0 |
| Speech recognition (live line) | Whisper large-v3-turbo | OpenAI | MIT |
| Translation, meeting minutes | Gemma 4 E4B-it (text part only) | Google | Apache 2.0 |
| Sentence splitting | Silero VAD | Silero | MIT |

All models come from the official Hugging Face repositories and can be used commercially.

## Requirements (server PC)

- Windows 10 / 11, 64-bit.
- NVIDIA GPU with **16 GB** of video memory: the models use about 13 GB. 12 GB works with
  `--light`, which costs a little accuracy. Tested on an RTX 5080 (16 GB).
- A recent NVIDIA driver (one that supports CUDA 12.8, i.e. version 570 or newer).
- Python 3.12 from [python.org](https://www.python.org/downloads/windows/). Versions 3.10–3.14 also
  work. In the installer, tick "Add python.exe to PATH".
- About 30 GB of free disk space: 20 GB for the models and 5 GB for the Python packages.
- Internet access during setup only (pytorch.org, pypi.org, huggingface.co). At run time nothing
  is downloaded.

## Setup (once)

1. Get the program into a short path such as `C:\RealTran`. Any one of these works:
   - Clone it with git:
     ```
     git clone https://github.com/sadeghdana/realtimescribe-main.git C:\RealTran
     ```
   - On the GitHub page, use **Code → Download ZIP** and unzip it.
   - Unzip `RealTran_server.zip`.

   A deep folder breaks the installation: some package files are nested ~150 characters deep, and
   Windows allows 260.
2. Double-click **`setup_server.bat`**. It:
   - creates a Python environment in `.venv`;
   - installs PyTorch and the other packages;
   - downloads the three models to `models\`;
   - checks every model file against the hashes Hugging Face publishes.

   This takes 20–60 minutes depending on the connection. If it is interrupted, run it again: it
   continues where it stopped.

   If huggingface.co is blocked on your network, copy the `models` folder from another RealTran
   server instead. Then check it with `.venv\Scripts\python.exe download_models.py --verify`.

## Updating

1. Get the new version:
   - **git:** run `git pull` in the RealTran folder.
   - **zip:** download it again and copy it over the old files, except your own `glossary.txt`.

   `models`, `.venv` and `meetings` are not touched.
2. Restart the server.
3. If `requirements.txt` changed, run `setup_server.bat` again first.

## Starting the server

In PowerShell, in the RealTran folder:

```
.\start.bat --no-local-audio
```

- Loading takes 30–60 seconds. The server is ready when the window prints `Ready.`
- Keep that window open: closing it stops the server.
- On the first start, Windows asks whether to allow network access. Allow it for private networks
  (this needs administrator rights), otherwise other PCs cannot connect. The port is TCP 8765.

Without `--no-local-audio`, the server also listens to the server PC's own microphone. Its page
then opens on that audio: `http://localhost:8766/?session=local`.

## Using it

**A. On the server PC itself (simplest)**

Once the server is ready, double-click `client\start_client_local.bat`. It captures this PC's
microphone and its system audio (the remote participants in Teams / Zoom / Meet). The subtitles
appear in an always-on-top bar at the bottom of the screen.

**B. On other PCs in the network**

1. On the server, run `.venv\Scripts\python.exe make_client_zip.py`. It creates
   `RealTran_client.zip` with this server's address already filled in.
2. Copy the zip to the other PCs and unzip it.
3. Run `client\start_client_lan.bat` (Windows) or `client/start_client_lan.sh` (Mac).

The other PCs need Python 3.10+. Details are in [client/README_EN.md](client/README_EN.md).

**C. Browser only (phones, PCs without Python)**

1. Open `https://<server address>:8765/`.
2. Accept the certificate warning once. It appears because the certificate is self-signed.
3. Click **🎤 Mic** to use that device's microphone.

There is no subtitle bar and no system audio in this mode.

**Several users**

- Every PC and every browser is its own session: its captions, history and meeting records are not
  shown to anyone else.
- One server handles several meetings at once:
  - 2–3 simultaneous meetings are fine.
  - With more people speaking at the same time, translations lag by about 1 second, up to about
    5 seconds at peaks.
- There is no login, so anyone on the LAN who knows a session name (e.g. `pc:<computer name>`) can
  open it in a browser. Use it on a trusted network only.

## Switching the interface to English

The interface starts in Japanese. You only need to switch it once per device.

- **Web page:** use the language selector at the top right. Choose which caption languages to show
  with the ZH / JA / EN buttons.
- **Subtitle bar:**
  1. Right-click it, then choose **表示言語 / 界面语言 / Language → English**.
  2. Right-click again and tick **Show English**. Untick the languages you do not need.

## Recording meetings and AI minutes

Nothing is saved unless you press **⏺ Record**: on the web page, or right-click the subtitle bar →
**Start recording the meeting**. From then on, your own session's sentences and their translations
are saved to `meetings\<date_time>\`.

When you stop, Gemma writes the minutes in the interface language: summary, topics, decisions,
to-dos and open issues. To see them, open the web page's **Minutes** tab, or right-click the
subtitle bar → **Open the minutes**. You can download the minutes and the transcript as `.md`, or
have the minutes rewritten in another language.

## Better accuracy: `glossary.txt`

Put the names, products and technical terms your team uses into `glossary.txt`, one per line,
optionally with their spelling in each language. They are given to both the speech recognition and
the translation. Restart the server after editing it.

## Options

Add these after `start.bat`.

| Option | Effect |
|---|---|
| `--no-local-audio` | Do not listen to the server PC's own microphone (audio comes from clients / browsers) |
| `--light` | Use Whisper turbo for the final text too: saves about 3 GB of VRAM, slightly less accurate |
| `--end-silence-ms 600` | Pause that ends a sentence; shorter = faster, but more fragmented sentences |
| `--commit-sentences 2` | Translate after this many finished sentences without waiting for a pause (0 = off) |
| `--no-translate` | Transcription only, no translation model |
| `--loopback` | Caption what the server PC plays, instead of its microphone |
| `--log-all` | Also log every sentence to `transcripts\`, not only while recording |

## Troubleshooting

- **Subtitles lag:** video memory is probably full. Close other programs that use the GPU, or start
  with `--light`.
- **`Ready.` never appears, or CUDA errors:** update the NVIDIA driver.
- **Other PCs cannot connect:**
  - Check that Windows Firewall allows TCP 8765 inbound.
  - Check that the PCs are on the same network.
  - Check that the address in their launcher is the server's current address. If it changed,
    rebuild the zip with `make_client_zip.py`.
- **Text is hidden behind the subtitle bar's background:** update the client to version
  2026.10.23-12 or newer.
- **Odd sentences when nobody speaks** (e.g. "Thanks for watching"): Whisper invents these from
  noise. Known ones are filtered in `realtran/asr.py`. New ones can be added to `HALLUCINATIONS`
  there.

## Files

```
setup_server.bat     one-time setup (Python environment + models)
download_models.py   downloads / verifies the models
start.bat            starts the server
make_client_zip.py   packs the desktop client for other PCs, with this server's address
make_server_zip.py   packs the server program for installing on another PC
app.py               server entry point
realtran/            speech recognition, segmentation, translation, minutes, web server
web/index.html       the web page
client/              desktop client (microphone + system audio -> subtitle bar)
glossary.txt         your names and terms
models/              model weights (created by setup)
meetings/            recorded meetings and minutes (created when you record)
```
