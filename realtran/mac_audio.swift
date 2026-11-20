// RealTran's audio helper on a Mac: what the Mac plays (the Speaker input), the microphone, and the
// English voice that reads translations aloud, all in this one process, so that the voice never
// comes back in as speech to transcribe:
//   - Speaker: ScreenCaptureKit with excludesCurrentProcessAudio, so the voice played here is
//     left out of the capture digitally (also with headphones, as the audio is taken before it
//     reaches any output device).
//   - Microphone: captured with macOS voice processing (the echo cancellation FaceTime uses).
//     The voice is played through the same audio engine, so it is the echo reference and is
//     subtracted from what the microphone hears through the air.
// Used by realtran/audio.py (MacAudioHelper). A separate process instead of Python bindings: the
// Python bridge crashed on ScreenCaptureKit's buffers now and then.
//
// stdin, one command per line: "system on|off", "mic on|off", "speak <text>", "hush" (stop speaking)
// stdout: frames of kind (1 byte: "S" system, "M" microphone) + length (UInt32 LE) + float32 PCM,
//         mono; system at 16 kHz, microphone at the rate given in "mic: ready <rate>"
// stderr: "ready", "system: ready", "mic: ready <rate>", "<what>: error: ...",
//         "speech: start", "speech: idle". Exits when stdin closes (the app went away).
import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

setvbuf(stderr, nil, _IONBF, 0)
signal(SIGPIPE, SIG_IGN)

func log(_ line: String) { fputs(line + "\n", stderr) }

let outQueue = DispatchQueue(label: "realtran.out")

func emit(_ kind: UInt8, _ samples: UnsafePointer<Float>, _ count: Int) {
    guard count > 0 else { return }
    var frame = Data([kind])
    var size = UInt32(count * 4).littleEndian
    frame.append(Data(bytes: &size, count: 4))
    frame.append(Data(bytes: samples, count: count * 4))
    outQueue.async {
        do { try FileHandle.standardOutput.write(contentsOf: frame) } catch { exit(0) }  // the app closed the pipe
    }
}

// ---- Speaker: what this Mac plays, minus this process's own sound (the voice) ----------------------

final class SystemCapture: NSObject, SCStreamOutput, SCStreamDelegate {
    var stream: SCStream?
    let queue = DispatchQueue(label: "realtran.system-audio")

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid,
              let block = CMSampleBufferGetDataBuffer(sampleBuffer) else { return }
        let length = CMBlockBufferGetDataLength(block)
        let count = length / 4
        var data = [Float](repeating: 0, count: count)
        let status = data.withUnsafeMutableBytes { raw in
            CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: count * 4, destination: raw.baseAddress!)
        }
        guard status == noErr else { return }
        data.withUnsafeBufferPointer { emit(UInt8(ascii: "S"), $0.baseAddress!, $0.count) }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        log("system: error: capture stopped: \(error.localizedDescription)")
        self.stream = nil
    }

    func start() async {
        guard stream == nil else { log("system: ready"); return }
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            guard let display = content.displays.first else { log("system: error: no display found"); return }
            let config = SCStreamConfiguration()
            config.capturesAudio = true
            config.sampleRate = 16000
            config.channelCount = 1
            config.excludesCurrentProcessAudio = true  // the voice reading translations is played here
            // The API needs a screen stream too: keep it tiny and rare; it is never used.
            config.width = 2
            config.height = 2
            config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
            config.queueDepth = 3
            let s = SCStream(filter: SCContentFilter(display: display, excludingWindows: []),
                             configuration: config, delegate: self)
            try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
            try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
            try await s.startCapture()
            stream = s
            log("system: ready")
        } catch {
            log("system: error: \(error.localizedDescription)")
        }
    }

    func stop() async {
        if let s = stream {
            stream = nil
            try? await s.stopCapture()
        }
    }
}

// ---- Microphone (echo-cancelled) and the voice, on one audio engine --------------------------------

final class AudioIO {
    var engine: AVAudioEngine?
    var player = AVAudioPlayerNode()
    var playerFormat: AVAudioFormat?  // the engine's own format; speech is converted to it
    var converters: [String: AVAudioConverter] = [:]
    var micOn = false
    let synth = AVSpeechSynthesizer()
    var pending = 0  // speech buffers scheduled and not played yet
    lazy var voice: AVSpeechSynthesisVoice? = {
        let english = AVSpeechSynthesisVoice.speechVoices().filter { $0.language == "en-US" }
        return english.max { $0.quality.rawValue < $1.quality.rawValue } ?? AVSpeechSynthesisVoice(language: "en-US")
    }()

    /// A fresh engine: voice processing can only be switched on or off on a stopped one, and only
    /// an engine with the microphone on touches the input device at all. The player is connected in
    /// the engine's own format before voice processing is enabled (another order fails to start).
    func rebuild(mic: Bool) {
        engine?.stop()
        if pending > 0 { pending = 0; log("speech: idle") }
        micOn = false
        let e = AVAudioEngine()
        player = AVAudioPlayerNode()
        e.attach(player)
        e.connect(player, to: e.mainMixerNode, format: nil)
        playerFormat = player.outputFormat(forBus: 0)
        converters = [:]
        engine = e
        guard mic else { return }
        let input = e.inputNode
        do {
            try input.setVoiceProcessingEnabled(true)
        } catch {
            log("mic: error: echo cancellation unavailable: \(error.localizedDescription)")
            engine = nil
            return
        }
        if #available(macOS 14.0, *) {  // do not turn other apps (the meeting) down while we listen
            input.voiceProcessingOtherAudioDuckingConfiguration =
                AVAudioVoiceProcessingOtherAudioDuckingConfiguration(enableAdvancedDucking: false, duckingLevel: .min)
        }
        let format = input.outputFormat(forBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
            guard let ch = buffer.floatChannelData else { return }
            emit(UInt8(ascii: "M"), ch[0], Int(buffer.frameLength))  // channel 0: the processed voice
        }
        do {
            e.prepare()
            try e.start()
            micOn = true
            log("mic: ready \(Int(format.sampleRate))")
        } catch {
            log("mic: error: \(error.localizedDescription)")
            input.removeTap(onBus: 0)
            engine = nil
        }
    }

    func setMic(_ on: Bool) {
        if on && micOn { log("mic: ready \(Int(engine!.inputNode.outputFormat(forBus: 0).sampleRate))"); return }
        if !on && !micOn { return }
        rebuild(mic: on)
    }

    func speak(_ text: String) {
        let u = AVSpeechUtterance(string: text)
        u.voice = voice
        u.rate = min(AVSpeechUtteranceMaximumSpeechRate, AVSpeechUtteranceDefaultSpeechRate * 1.08)
        synth.write(u) { buffer in
            guard let pcm = buffer as? AVAudioPCMBuffer, pcm.frameLength > 0 else { return }
            DispatchQueue.main.async { self.play(pcm) }
        }
    }

    func play(_ speech: AVAudioPCMBuffer) {
        if engine == nil { rebuild(mic: false) }
        guard let e = engine, let target = playerFormat, let pcm = convert(speech, to: target) else { return }
        if !e.isRunning {
            e.prepare()
            do { try e.start() } catch { log("speech: error: \(error.localizedDescription)"); return }
        }
        if pending == 0 { log("speech: start") }
        pending += 1
        let p = player
        p.scheduleBuffer(pcm) {
            DispatchQueue.main.async {
                guard p === self.player, self.pending > 0 else { return }
                self.pending -= 1
                if self.pending == 0 { log("speech: idle") }
            }
        }
        if !p.isPlaying { p.play() }
    }

    func hush() {
        synth.stopSpeaking(at: .immediate)
        player.stop()
        if pending > 0 { pending = 0; log("speech: idle") }
    }

    /// Speech arrives as 16-bit or float PCM at the voice's own rate; the player plays the
    /// engine's format.
    func convert(_ pcm: AVAudioPCMBuffer, to target: AVAudioFormat) -> AVAudioPCMBuffer? {
        if pcm.format == target { return pcm }
        let key = "\(pcm.format)"
        guard let conv = converters[key] ?? AVAudioConverter(from: pcm.format, to: target) else { return nil }
        converters[key] = conv
        let capacity = AVAudioFrameCount(Double(pcm.frameLength) * target.sampleRate / pcm.format.sampleRate) + 1024
        guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return nil }
        var fed = false
        var error: NSError?
        let status = conv.convert(to: out, error: &error) { _, inputStatus in
            if fed { inputStatus.pointee = .noDataNow; return nil }
            fed = true
            inputStatus.pointee = .haveData
            return pcm
        }
        return status == .error ? nil : out
    }
}

let system = SystemCapture()
let audio = AudioIO()

func handle(_ line: String) {
    let parts = line.split(separator: " ", maxSplits: 1).map(String.init)
    guard let cmd = parts.first else { return }
    let arg = parts.count > 1 ? parts[1] : ""
    switch (cmd, arg) {
    case ("system", "on"): Task { await system.start() }
    case ("system", "off"): Task { await system.stop() }
    case ("mic", "on"): audio.setMic(true)
    case ("mic", "off"): audio.setMic(false)
    case ("speak", _): if !arg.isEmpty { audio.speak(arg) }
    case ("hush", _): audio.hush()
    default: log("error: unknown command \(line)")
    }
}

Thread {
    while let line = readLine() {
        let cmd = line.trimmingCharacters(in: .whitespaces)
        DispatchQueue.main.async { handle(cmd) }
    }
    exit(0)  // stdin closed: the app is gone
}.start()

log("ready")
dispatchMain()
