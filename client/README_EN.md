# RealTran desktop client

> **日本語:** [README.md](README.md)

Run this on the PC you attend the meeting with. It:

- captures two audio sources:
  - **this PC's microphone**: you, and the people in the room;
  - **its system audio**: the remote participants in Teams / Zoom / Meet;
- sends both to the RealTran server, which transcribes and translates them;
- shows the subtitles in an always-on-top bar at the bottom of the screen, above the meeting app.

This PC does not need a GPU, only Python.

## First use

1. Install Python 3.10 or newer from [python.org](https://www.python.org/downloads/).
   - **Windows:** tick "Add python.exe to PATH" in the installer.
   - **Mac:** use the python.org installer, which includes the Tk library the subtitle bar needs.
     Homebrew's Python does not include it.
2. Unzip `RealTran_client.zip` (made on the server with `make_client_zip.py`, so it already
   contains the server's address).
3. Make sure the server is running.

## Starting

- **Windows:** double-click `start_client_lan.bat`. In PowerShell, type `.\start_client_lan.bat`.
- **Mac:** in Terminal, in the `client` folder, run `sh start_client_lan.sh`. Allow microphone
  access when macOS asks.
- **On the server PC itself:** use `start_client_local.bat`.
- `start_client.bat` / `.sh` connect through an SSH tunnel instead. They need OpenSSH Server and key
  login on the server; the `_lan` launchers do not.

The first start installs the required packages, which takes about a minute. The client accepts the
server's self-signed certificate by itself.

The subtitle bar's top line shows the status:

```
● Connected   Mic ▮▮▯▯▯   System audio ▮▮▮▯▯
```

The meters move when someone speaks. Each sentence is tagged **Local** (microphone) or **Online**
(system audio).

## Using the subtitle bar

- **Drag** it with the left mouse button. Its position is remembered.
- **Mouse wheel:** click the bar once, then scroll to change the background opacity.
- **Right-click** opens the settings. All settings are saved in `client_settings.json`.
  - **表示言語 / 界面语言 / Language:** switch the menus to English first. The interface starts in
    Japanese.
  - **Show Chinese / Japanese / English:** which translations to show. Several can be shown at once.
  - **Also show the original:** the original sentence in grey under the translation.
  - **Background opacity:** from opaque to fully transparent (text only, with an outline).
  - **Sentences shown**, **text size**, **width**.
  - **Capture: microphone / system audio:** turn either source off.
  - **⏺ Start recording the meeting / ⏹ Stop recording and write the minutes:** from Start on, your
    sentences are saved. When you stop, the server writes the minutes.
  - **Open the minutes (browser).**

Your microphone, system audio, captions and recordings belong to this PC only. Other users of the
same server never see them.

## Which devices are used

- **Microphone:** follows Windows' default microphone. If you plug in a headset, it switches within
  a few seconds. The menu shows the device in use. To always use one microphone, start with
  `--mic-device <part of its name>`.
- **System audio:** all playback devices are captured and mixed, so Teams on the headset and a
  browser on the speakers are both caught. Run `start_client_lan.bat --list-devices` to see them.
- **Speakers instead of a headset:** the microphone also hears the remote participants, which
  would produce every sentence twice. To prevent that, the client mutes the microphone while the
  system audio has speech. With a headset you can turn this off with `--no-duck`.

## Mac

- **System audio:** macOS cannot record what it plays without a virtual audio device.
  1. Install the free **BlackHole 2ch**.
  2. In *Audio MIDI Setup*, create a Multi-Output Device with your speakers / headphones and
     BlackHole.
  3. Set that Multi-Output Device as the system output.

  The client finds BlackHole automatically. Without it, only the microphone is captured.
- **Opacity:** text and background cannot be separate layers on macOS, so the text fades along with
  the background.
- **Right-click:** two-finger click, or Control-click.
- **Full-screen meetings:** the subtitle bar cannot appear over a full-screen Zoom or Teams. Use
  them in a window.

## Options

| Option | Effect |
|---|---|
| `--list-devices` | List microphones and speakers |
| `--test-audio` | Record 5 s from every speaker and microphone and report which ones receive sound. Play something while it runs |
| `--mic-device NAME` | Use this microphone (part of its name is enough) |
| `--system-device NAME` | Use this input instead of the system audio (on a Mac, BlackHole is found automatically) |
| `--no-duck` | With a headset: do not mute the microphone while remote participants speak |
| `--server wss://ADDRESS:8765` | Connect to a different server |
