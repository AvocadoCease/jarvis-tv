// Jarvis (dictation): hold fn, speak, release. Local Whisper (whisper-server on 127.0.0.1:8178)
// transcribes and the text is pasted into whatever has focus.
//
//   hold fn          record while held, transcribe on release
//   double-tap fn    hands-free: record until the next fn tap
//   fn + any key     ignored (so fn+F1, fn+arrows etc. still work normally)
//   hold Right Option  ask JARVIS (calendar, email, Jira, the TV...): it answers out loud
//   double-tap Right Option  ask without holding: it listens until you pause
//   after an answer  it keeps listening a few seconds, so you can just reply ("that's all" ends it)
//
// Needs: Microphone, Accessibility (to paste) and Input Monitoring (to see fn).

import AVFoundation
import ApplicationServices
import Cocoa

let serverURL = URL(string: "http://127.0.0.1:8178/inference")!
let fnKeyCode: Int64 = 63          // kVK_Function (also the Globe key)
let minHold: TimeInterval = 0.4    // shorter presses are taps, not dictation
let minSpeechLevel: Float = 0.005  // loudest moment must reach this, or it was just room noise
let doubleTapWindow: TimeInterval = 0.6  // from the first tap's release to the second press
let escapeKeyCode: Int64 = 53
let handsFreeGrace: TimeInterval = 1.0   // taps this soon after starting hands-free are stray extra taps
let afterStopIgnore: TimeInterval = 0.7  // so double-tapping to stop doesn't start a new take

// Ask JARVIS: hold Right Option. The JARVIS server (tools/serve.py) answers.
let jarvisServer = "http://127.0.0.1"
let rightOptionKeyCode: Int64 = 61                 // kVK_RightOption
let rightOptionMask: UInt64 = 0x40                 // NX_DEVICERALTKEYMASK
let askMinHold: TimeInterval = 0.35                // shorter is a tap (or an Option+key combo)
let ackDelay: TimeInterval = 3.5                   // "one moment" only if nothing at all has been said by then
let acknowledgements = ["One moment.", "Let me check.", "Just a moment.", "Checking now."]

// Talking with JARVIS without holding the key
let followUpWait: TimeInterval = 6                 // after an answer, how long to wait for a reply to start
let handsFreeAskWait: TimeInterval = 8             // after a double-tap of Right Option
let endOfSpeechPause: TimeInterval = 1.1           // this much quiet after you speak ends the question
let maxQuestionSeconds: TimeInterval = 30
let handsFreeMinPeak: Float = 0.02                 // a reply must be this loud: the TV across the room isn't you

/// "That's all", "thanks, that's it", "goodbye"...: done talking to JARVIS for now.
func isConversationEnd(_ text: String) -> Bool {
    let t = text.lowercased().trimmingCharacters(in: .punctuationCharacters.union(.whitespaces))
    let pattern = #"^((ok(ay)?|no|nope|thanks|thank you|cheers|great|perfect)[ ,.!]*)*"#
        + #"(that'?s (all|it|everything)|i'?m (done|good)|goodbye|bye|stop listening|never ?mind|nothing|no thanks|"#
        + #"all good|thanks|thank you)([ ,.!]+(for now|thanks|thank you|jarvis))*$"#
    return t.range(of: pattern, options: .regularExpression) != nil
}

// MARK: - Audio

final class Recorder {
    private let engine = AVAudioEngine()
    private var samples: [Float] = []
    private var rate: Double = 48_000
    private let lock = NSLock()
    private(set) var level: Float = 0
    private(set) var peak: Float = 0
    private(set) var running = false

    func start() throws {
        lock.lock(); samples.removeAll(keepingCapacity: true); lock.unlock()
        peak = 0
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        rate = format.sampleRate
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            guard let self, let channel = buffer.floatChannelData?[0] else { return }
            let n = Int(buffer.frameLength)
            var sum: Float = 0
            self.lock.lock()
            for i in 0..<n {
                let v = channel[i]
                self.samples.append(v)
                sum += v * v
            }
            self.lock.unlock()
            self.level = sqrt(sum / Float(max(n, 1)))
            self.peak = max(self.peak, self.level)
        }
        engine.prepare()
        try engine.start()
        running = true
    }

    /// Start a fresh take without stopping the mic (no warm-up gap).
    func restartTake() {
        lock.lock(); samples.removeAll(keepingCapacity: true); lock.unlock()
        peak = 0
    }

    /// Stop and return 16 kHz mono 16-bit WAV, which is what Whisper wants.
    func stop() -> (wav: Data, seconds: Double) {
        if running {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
            running = false
        }
        level = 0
        lock.lock(); let captured = samples; lock.unlock()
        let pcm = Recorder.resample(captured, from: rate, to: 16_000)
        return (Recorder.wav(pcm), Double(pcm.count) / 16_000)
    }

    private static func resample(_ x: [Float], from: Double, to: Double) -> [Int16] {
        guard !x.isEmpty else { return [] }
        let ratio = from / to
        let count = Int(Double(x.count) / ratio)
        var out = [Int16](repeating: 0, count: count)
        for i in 0..<count {
            let pos = Double(i) * ratio
            let j = Int(pos)
            let frac = Float(pos - Double(j))
            let a = x[min(j, x.count - 1)], b = x[min(j + 1, x.count - 1)]
            let v = max(-1, min(1, a + (b - a) * frac))
            out[i] = Int16(v * 32_767)
        }
        return out
    }

    private static func wav(_ pcm: [Int16]) -> Data {
        var d = Data()
        func u32(_ v: UInt32) { var le = v.littleEndian; d.append(Data(bytes: &le, count: 4)) }
        func u16(_ v: UInt16) { var le = v.littleEndian; d.append(Data(bytes: &le, count: 2)) }
        let bytes = UInt32(pcm.count * 2)
        d.append("RIFF".data(using: .ascii)!); u32(36 + bytes)
        d.append("WAVEfmt ".data(using: .ascii)!); u32(16); u16(1); u16(1)
        u32(16_000); u32(32_000); u16(2); u16(16)
        d.append("data".data(using: .ascii)!); u32(bytes)
        pcm.withUnsafeBufferPointer { d.append(Data(buffer: $0)) }
        return d
    }
}

// MARK: - Transcription

func transcribe(_ wav: Data, completion: @escaping (Result<String, Error>) -> Void) {
    var req = URLRequest(url: serverURL)
    req.httpMethod = "POST"
    req.timeoutInterval = 60
    let boundary = "dictate-\(UUID().uuidString)"
    req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
    var body = Data()
    func field(_ name: String, _ value: String) {
        body.append("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"\r\n\r\n\(value)\r\n".data(using: .utf8)!)
    }
    field("response_format", "json")
    field("temperature", "0.0")
    body.append("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"speech.wav\"\r\nContent-Type: audio/wav\r\n\r\n".data(using: .utf8)!)
    body.append(wav)
    body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)
    req.httpBody = body

    URLSession.shared.dataTask(with: req) { data, _, error in
        if let error { return completion(.failure(error)) }
        guard let data,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let text = json["text"] as? String else {
            return completion(.failure(NSError(domain: "Dictate", code: 1,
                userInfo: [NSLocalizedDescriptionKey: "Whisper sent an unexpected reply"])))
        }
        completion(.success(clean(text)))
    }.resume()
}

/// Personal spellings Whisper gets wrong: ~/.config/jarvis/replacements.json, {"heard": "wanted"}.
/// Re-read each time, so edits apply without restarting.
func replacements() -> [String: String] {
    let url = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".config/jarvis/replacements.json")
    guard let data = try? Data(contentsOf: url),
          let map = try? JSONSerialization.jsonObject(with: data) as? [String: String] else { return [:] }
    return map
}

/// Whisper marks silence and noises in brackets; drop those and tidy spacing.
func clean(_ text: String) -> String {
    var t = text.replacingOccurrences(of: #"\[[^\]]*\]|\([^)]*(music|silence|noise|inaudible|blank)[^)]*\)"#,
                                      with: "", options: [.regularExpression, .caseInsensitive])
    t = t.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
    for (heard, wanted) in replacements() {
        let pattern = "\\b" + NSRegularExpression.escapedPattern(for: heard) + "\\b"
        t = t.replacingOccurrences(of: pattern, with: wanted, options: .regularExpression)
    }
    return t.trimmingCharacters(in: .whitespacesAndNewlines)
}

/// Phrases Whisper is known to make up from silence or breath on very short clips.
func isHallucination(_ text: String, seconds: Double) -> Bool {
    guard seconds < 2.5 else { return false }
    let t = text.lowercased().trimmingCharacters(in: .punctuationCharacters.union(.whitespaces))
    return ["thank you", "thanks", "thank you very much", "thanks for watching", "thank you for watching",
            "you", "bye", "okay", "so", "um", "uh", "hmm", "the end", "subtitles by the amaraorg community"].contains(t)
}

// MARK: - fn -> F18 remap (hidutil), so macOS never sees fn while Jarvis runs

let f18KeyCode: Int64 = 79   // kVK_F18
// fn on Apple keyboards: Apple vendor top-case page 0x00FF and keyboard page 0xFF01, usage 0x03.
// F18 is HID keyboard page 0x07, usage 0x6D.
let fnRemap = #"{"UserKeyMapping":[{"HIDKeyboardModifierMappingSrc":0xFF00000003,"HIDKeyboardModifierMappingDst":0x70000006D},{"HIDKeyboardModifierMappingSrc":0xFF0100000003,"HIDKeyboardModifierMappingDst":0x70000006D}]}"#

@discardableResult
func hidutil(_ json: String) -> Bool {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/hidutil")
    p.arguments = ["property", "--set", json]
    p.standardOutput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    do { try p.run(); p.waitUntilExit(); return p.terminationStatus == 0 } catch { return false }
}

func takeOverFnKey() { log("fn remapped to F18: \(hidutil(fnRemap))") }
func releaseFnKey() { log("fn remap removed: \(hidutil(#"{"UserKeyMapping":[]}"#))") }

// MARK: - Log (~/Library/Logs/Dictate/jarvis-app.log)

let logURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/Dictate/jarvis-app.log")
let logStart = Date()
func log(_ message: String) {
    let line = String(format: "%8.3f  ", Date().timeIntervalSince(logStart)) + message + "\n"
    if let h = try? FileHandle(forWritingTo: logURL) {
        h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); try? h.close()
    } else {
        try? line.data(using: .utf8)!.write(to: logURL)
    }
}

// MARK: - Is the cursor in something we can type into?

func focusedElementIsEditable() -> (editable: Bool, role: String) {
    guard let el = focusedElement() else { return (false, "none") }
    var roleValue: AnyObject?
    AXUIElementCopyAttributeValue(el, kAXRoleAttribute as CFString, &roleValue)
    let role = roleValue as? String ?? "?"
    if ["AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"].contains(role) { return (true, role) }
    var settable: DarwinBoolean = false
    if AXUIElementIsAttributeSettable(el, kAXValueAttribute as CFString, &settable) == .success, settable.boolValue {
        return (true, role)
    }
    var range: AnyObject?
    if AXUIElementCopyAttributeValue(el, kAXSelectedTextRangeAttribute as CFString, &range) == .success { return (true, role) }
    return (false, role)
}

// MARK: - The emoji picker (macOS opens it on any fn tap; we close it before pasting)

let syntheticMarker: Int64 = 0x4A41_5256  // "JARV": tags key presses Jarvis sends itself

func emojiPickerOpen() -> Bool {
    let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
        as? [[String: Any]] ?? []
    return windows.contains { w in
        guard (w[kCGWindowOwnerName as String] as? String) == "Emoji & Symbols",
              let b = w[kCGWindowBounds as String] as? [String: Any],
              let width = b["Width"] as? Double else { return false }
        return width > 100
    }
}

func emojiPickerPIDs() -> Set<pid_t> {
    let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
    return Set(windows.filter { ($0[kCGWindowOwnerName as String] as? String) == "Emoji & Symbols" }
        .compactMap { $0[kCGWindowOwnerPID as String] as? pid_t })
}

/// Close the picker, then call `done` once it has really gone (Esc can take ~0.5 s;
/// if it's still up after that, quit its helper, which macOS relaunches on demand).
func closeEmojiPicker(then done: @escaping () -> Void) {
    guard emojiPickerOpen() else { return done() }
    dismissEmojiPicker()
    var waited = 0.0
    func check() {
        if !emojiPickerOpen() { log("  emoji picker gone after \(waited)s"); return done() }
        if waited >= 0.6 {
            for pid in emojiPickerPIDs() { NSRunningApplication(processIdentifier: pid)?.terminate() }
            log("  emoji picker slow to close: quit its helper")
        }
        if waited >= 1.5 { return done() }
        waited += 0.1
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1, execute: check)
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.1, execute: check)
}

/// Close the picker with Esc, but only if it's actually showing (Esc must never reach other apps).
@discardableResult
func dismissEmojiPicker() -> Bool {
    guard emojiPickerOpen() else { return false }
    let src = CGEventSource(stateID: .combinedSessionState)
    for keyDown in [true, false] {
        let e = CGEvent(keyboardEventSource: src, virtualKey: 53, keyDown: keyDown)
        e?.setIntegerValueField(.eventSourceUserData, value: syntheticMarker)
        e?.post(tap: .cghidEventTap)
    }
    log("  closed the emoji picker")
    return true
}

// MARK: - Where you were typing when you started (to return there after the emoji picker)

/// The focused UI element: asked of the whole system, then of the front app directly
/// (Electron apps sometimes answer one and not the other).
func focusedElement() -> AXUIElement? {
    let front = NSWorkspace.shared.frontmostApplication
    wakeAccessibility(of: front)
    var focused: AnyObject?
    if AXUIElementCopyAttributeValue(AXUIElementCreateSystemWide(), kAXFocusedUIElementAttribute as CFString,
                                     &focused) == .success, let el = focused { return (el as! AXUIElement) }
    if let front, AXUIElementCopyAttributeValue(AXUIElementCreateApplication(front.processIdentifier),
                                                kAXFocusedUIElementAttribute as CFString, &focused) == .success,
       let el = focused { return (el as! AXUIElement) }
    return nil
}

/// Electron/Chromium apps (T3 Code, Slack, VS Code...) don't build their accessibility tree until an
/// assistive app asks, so their text boxes are invisible and dictation would fall back to the Copy
/// popup. Other dictation apps ask; we must too. Native apps just ignore the attribute.
var accessibilityWoken = Set<pid_t>()
func wakeAccessibility(of app: NSRunningApplication?) {
    guard let app, !accessibilityWoken.contains(app.processIdentifier) else { return }
    let result = AXUIElementSetAttributeValue(AXUIElementCreateApplication(app.processIdentifier),
                                              "AXManualAccessibility" as CFString, kCFBooleanTrue)
    if result == .success {
        accessibilityWoken.insert(app.processIdentifier)
        log("  accessibility woken for \(app.localizedName ?? "?")")
    }
}

func isEditable(_ el: AXUIElement) -> Bool {
    var roleValue: AnyObject?
    AXUIElementCopyAttributeValue(el, kAXRoleAttribute as CFString, &roleValue)
    if ["AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"].contains(roleValue as? String ?? "") { return true }
    var settable: DarwinBoolean = false
    return AXUIElementIsAttributeSettable(el, kAXValueAttribute as CFString, &settable) == .success && settable.boolValue
}

// MARK: - Transcript history (~/.config/jarvis/history.txt), so nothing is ever lost

let historyURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".config/jarvis/history.txt")
func saveToHistory(_ text: String) {
    let stamp = ISO8601DateFormatter().string(from: Date())
    let line = "\(stamp)\t\(text.replacingOccurrences(of: "\n", with: " "))\n"
    try? FileManager.default.createDirectory(at: historyURL.deletingLastPathComponent(), withIntermediateDirectories: true)
    if let h = try? FileHandle(forWritingTo: historyURL) {
        h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); try? h.close()
    } else {
        try? line.data(using: .utf8)!.write(to: historyURL)
    }
}

func copyToClipboard(_ text: String) {
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(text, forType: .string)
}

// MARK: - Paste into the focused app

func paste(_ text: String) {
    let pb = NSPasteboard.general
    let saved: [NSPasteboardItem] = (pb.pasteboardItems ?? []).map { item in
        let copy = NSPasteboardItem()
        for type in item.types { if let d = item.data(forType: type) { copy.setData(d, forType: type) } }
        return copy
    }
    // A trailing space after a full sentence, so the next dictation doesn't run on.
    let insert = text.range(of: #"[.!?]$"#, options: .regularExpression) != nil ? text + " " : text
    pb.clearContents()
    pb.setString(insert, forType: .string)

    let src = CGEventSource(stateID: .combinedSessionState)
    for keyDown in [true, false] {
        let e = CGEvent(keyboardEventSource: src, virtualKey: 9 /* v */, keyDown: keyDown)
        e?.flags = .maskCommand
        e?.post(tap: .cghidEventTap)
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) {
        pb.clearContents()
        if !saved.isEmpty { pb.writeObjects(saved) }
    }
}

// MARK: - JARVIS's voice on the Mac

func jarvisURL(_ path: String, _ params: [String: String]) -> URL {
    var c = URLComponents(string: jarvisServer + path)!
    c.queryItems = params.map { URLQueryItem(name: $0.key, value: $0.value) }
    c.percentEncodedQuery = c.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B")   // else "C++" arrives as "C  "
    return c.url!
}

/// What's in front of you, sent with a question so "what does this mean?" works anywhere:
/// the app, its window title, and any selected text (never from a password field).
func screenContext() -> [String: String] {
    guard let front = NSWorkspace.shared.frontmostApplication else { return [:] }
    var context = ["app": front.localizedName ?? ""]
    let app = AXUIElementCreateApplication(front.processIdentifier)
    var window: AnyObject?, title: AnyObject?
    if AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute as CFString, &window) == .success, let window,
       AXUIElementCopyAttributeValue(window as! AXUIElement, kAXTitleAttribute as CFString, &title) == .success,
       let title = title as? String, !title.isEmpty {
        context["window"] = String(title.prefix(200))
    }
    if let el = focusedElement() {
        var role: AnyObject?, subrole: AnyObject?, selected: AnyObject?
        AXUIElementCopyAttributeValue(el, kAXRoleAttribute as CFString, &role)
        AXUIElementCopyAttributeValue(el, kAXSubroleAttribute as CFString, &subrole)
        let secret = (subrole as? String) == (kAXSecureTextFieldSubrole as String)
        if !secret, AXUIElementCopyAttributeValue(el, kAXSelectedTextAttribute as CFString, &selected) == .success,
           let text = (selected as? String)?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty {
            context["selected"] = String(text.prefix(1500))
        }
    }
    return context
}

final class Speaker: NSObject, AVAudioPlayerDelegate {
    private var player: AVAudioPlayer?
    private var queue: [Data] = []
    private var cache: [String: Data] = [:]
    private var generation = 0          // bumped by stop(), so audio still being fetched is dropped
    private var texts: [String] = []    // sentences waiting for their voice clip, in order
    private var fetchingNow = false     // one clip on its way (fetched while the one before plays)
    var onIdle: (() -> Void)?

    var speaking: Bool { player?.isPlaying ?? false }
    /// Speaking, or about to: a clip is still on its way.
    var busy: Bool { speaking || fetchingNow || !texts.isEmpty || !queue.isEmpty }

    /// Speak `text` after anything already playing. Streamed answers call this once per sentence,
    /// as each arrives; they're spoken in that order, each fetched while the one before plays.
    func say(_ text: String) {
        texts.append(text)
        pump()
    }

    private func pump() {
        guard !fetchingNow else { return }
        guard !texts.isEmpty else {
            if !busy { onIdle?() }
            return
        }
        let text = texts.removeFirst()
        let gen = generation
        fetchingNow = true
        fetch(text) { data in
            guard gen == self.generation else { return }   // stopped meanwhile
            self.fetchingNow = false
            if let data { self.enqueue(data) }
            self.pump()
        }
    }

    /// The voice for `text` (short phrases cached), on the main queue; nil if the server failed.
    private func fetch(_ text: String, _ done: @escaping (Data?) -> Void) {
        if let d = cache[text] { return done(d) }
        URLSession.shared.dataTask(with: jarvisURL("/api/tts", ["text": text, "from": "mac"])) { data, response, _ in
            let ok = data != nil && (response as? HTTPURLResponse)?.mimeType == "audio/mpeg"
            DispatchQueue.main.async {
                if ok, let data, text.count < 40 { self.cache[text] = data }
                done(ok ? data : nil)
            }
        }.resume()
    }

    /// Fetch short phrases ahead of time so they play instantly.
    /// Called again every few minutes, so a change of voice on the server reaches the saved clips too.
    func preload(_ phrases: [String]) {
        cache = cache.filter { phrases.contains($0.key) }   // drop other saved clips; they refill on use
        for text in phrases {
            URLSession.shared.dataTask(with: jarvisURL("/api/tts", ["text": text, "from": "mac"])) { data, _, _ in
                if let data { DispatchQueue.main.async { self.cache[text] = data } }
            }.resume()
        }
    }

    private func enqueue(_ data: Data) {
        queue.append(data)
        if !speaking { next() }
    }

    private func next() {
        guard !queue.isEmpty else {
            player = nil
            if !fetchingNow && texts.isEmpty { onIdle?() }     // otherwise the next clip is still coming
            return
        }
        player = try? AVAudioPlayer(data: queue.removeFirst())
        player?.delegate = self
        if player?.play() != true { next() }
    }

    func stop() {
        generation += 1
        texts.removeAll()
        fetchingNow = false
        queue.removeAll()
        player?.stop()
        player = nil
    }

    func audioPlayerDidFinishPlaying(_ p: AVAudioPlayer, successfully flag: Bool) { next() }
}

struct JarvisAnswer {
    let reply: String
    let details: String?   // set when JARVIS is asking "shall I?" about an action: exactly what it would do
    let speak: String      // what's still to be said (streamed sentences were spoken as they came)
}

/// Send a question (or command) to JARVIS, streamed (/api/ask). While it works, `onEvent` gets
/// ("status", "Checking your calendar.") and ("say", each sentence as it's written); then `completion`
/// gets the answer (nil if the server failed). Both on the main queue. Cancel the task to drop it.
@discardableResult
func askJarvis(_ text: String, screen: [String: String] = [:], onEvent: @escaping (String, String) -> Void,
               completion: @escaping (JarvisAnswer?) -> Void) -> Task<Void, Never> {
    var req = URLRequest(url: jarvisURL("/api/ask", screen.merging(["text": text, "from": "mac"]) { $1 }))
    req.timeoutInterval = 100
    return Task {
        var answer: JarvisAnswer?
        do {
            let (bytes, _) = try await URLSession.shared.bytes(for: req)
            for try await line in bytes.lines {
                guard let json = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any],
                      let type = json["type"] as? String else { continue }
                if type == "done" {
                    let reply = json["reply"] as? String ?? ""
                    answer = JarvisAnswer(reply: reply,
                                          details: json["confirm"] as? Bool == true ? json["details"] as? String : nil,
                                          speak: json["speak"] as? String ?? reply)
                } else if let t = json["text"] as? String {
                    await MainActor.run { onEvent(type, t) }
                }
            }
        } catch {
            log("  ask stream failed: \(error.localizedDescription)")
        }
        if Task.isCancelled { return }
        let result = answer
        await MainActor.run { completion(result) }
    }
}

// MARK: - On-screen indicator (a small dark pill with a live waveform)

enum PillMode { case listening, handsFree, transcribing, thinking }

final class Bubble {
    private var panel: NSPanel!
    private var pill = NSView()
    private var stop = NSView()
    private var bars: [NSView] = []
    private var generation = 0
    private var history = [CGFloat](repeating: 0, count: 14)
    private var timer: Timer?
    private var mode = PillMode.listening
    var levelSource: (() -> Float)?
    /// Bar colour: white for dictation, JARVIS cyan when you're asking it something.
    var asking = false

    private let barCount = 14
    private let barWidth: CGFloat = 3, barGap: CGFloat = 3
    private let height: CGFloat = 34

    init() { build() }

    // Builds (or rebuilds) the floating window. After Jarvis has run for a while macOS can
    // leave the old one stuck off-screen, so show() rebuilds it when that happens.
    private func build() {
        panel?.orderOut(nil)
        pill = NSView(); stop = NSView(); bars = []
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 160, height: 34),
                        styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.level = .statusBar
        panel.ignoresMouseEvents = true
        panel.hasShadow = true
        panel.alphaValue = 0
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.contentView = NSView()

        pill.wantsLayer = true
        pill.layer?.backgroundColor = NSColor(white: 0.04, alpha: 0.92).cgColor
        pill.layer?.cornerRadius = height / 2
        pill.layer?.borderWidth = 1
        pill.layer?.borderColor = NSColor(white: 1, alpha: 0.14).cgColor
        panel.contentView!.addSubview(pill)

        for _ in 0..<barCount {
            let bar = NSView()
            bar.wantsLayer = true
            bar.layer?.cornerRadius = barWidth / 2
            bar.layer?.backgroundColor = NSColor(white: 1, alpha: 0.92).cgColor
            pill.addSubview(bar)
            bars.append(bar)
        }

        stop.wantsLayer = true
        stop.layer?.cornerRadius = 2.5
        stop.layer?.backgroundColor = NSColor(white: 1, alpha: 0.85).cgColor
        pill.addSubview(stop)
    }

    private func layout() {
        let wave = CGFloat(barCount) * (barWidth + barGap) - barGap
        let width = 22 + wave + (mode == .handsFree ? 26 : 0) + 22
        let screen = NSScreen.screens.first { NSMouseInRect(NSEvent.mouseLocation, $0.frame, false) } ?? NSScreen.main!
        let f = screen.visibleFrame
        panel.setFrame(NSRect(x: f.midX - width / 2, y: f.minY + 22, width: width, height: height), display: false)
        pill.frame = NSRect(x: 0, y: 0, width: width, height: height)
        stop.isHidden = mode != .handsFree
        stop.frame = NSRect(x: width - 22 - 10, y: (height - 10) / 2, width: 10, height: 10)
    }

    func show(_ mode: PillMode) {
        self.mode = mode
        if mode != .transcribing && mode != .thinking { history = [CGFloat](repeating: 0, count: barCount) }
        generation += 1
        let shown = generation
        layout()
        panel.orderFrontRegardless()
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0.15
            panel.animator().alphaValue = 1
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in
            guard let self, self.generation == shown, !self.onScreen else { return }
            log("  bubble was stuck off-screen: rebuilding it")
            self.build()
            self.layout()
            self.panel.alphaValue = 1
            self.panel.orderFrontRegardless()
        }
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 1.0 / 40, repeats: true) { [weak self] _ in self?.tick() }
    }

    private func tick() {
        let t = Date().timeIntervalSince1970
        var heights = [CGFloat](repeating: 3, count: barCount)
        if mode == .transcribing || mode == .thinking {
            // Soft travelling wave while Whisper works
            for i in 0..<barCount {
                heights[i] = 4 + 7 * CGFloat(max(0, sin(Double(i) * 0.6 - t * 7)))
            }
        } else {
            // Scrolling waveform of your voice: newest on the right
            let level = CGFloat(min(1, (levelSource?() ?? 0) * 14))
            history.removeFirst()
            history.append(level)
            for i in 0..<barCount {
                heights[i] = 3 + history[i] * 20 * CGFloat(0.75 + 0.25 * sin(Double(i) * 2.1 + t * 11))
            }
        }
        let alpha: CGFloat = mode == .transcribing ? 0.55 : 0.92
        let color: NSColor = mode == .thinking ? NSColor(red: 1, green: 0.64, blue: 0.1, alpha: 0.9)   // JARVIS amber
            : asking ? NSColor(red: 0.09, green: 0.88, blue: 1, alpha: alpha)                            // JARVIS cyan
            : NSColor(white: 1, alpha: alpha)
        for (i, bar) in bars.enumerated() {
            let h = min(heights[i], height - 10)
            bar.frame = NSRect(x: 22 + CGFloat(i) * (barWidth + barGap), y: (height - h) / 2, width: barWidth, height: h)
            bar.layer?.backgroundColor = color.cgColor
        }
    }

    func hide() {
        timer?.invalidate()
        generation += 1
        let hidden = generation
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.18
            panel.animator().alphaValue = 0
        }, completionHandler: { [weak self] in
            // Only if nothing showed the bubble again while it was fading out
            if let self, self.generation == hidden { self.panel.orderOut(nil) }
        })
    }

    /// Whether the window server actually has the bubble on screen.
    private var onScreen: Bool {
        let info = CGWindowListCopyWindowInfo([.optionIncludingWindow], CGWindowID(panel.windowNumber)) as? [[String: Any]]
        return (info?.first?[kCGWindowIsOnscreen as String] as? Bool) ?? false
    }
}

// MARK: - Copy popup: shown when there's no text field to paste into

final class CopyToast: NSObject {
    private let panel: NSPanel
    private let label = NSTextField(wrappingLabelWithString: "")
    private let button = NSButton(title: "Copy", target: nil, action: nil)
    private var text = ""
    private var hideWork: DispatchWorkItem?

    override init() {
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 420, height: 64),
                        styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        super.init()
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.level = .statusBar
        panel.hasShadow = true
        panel.becomesKeyOnlyIfNeeded = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        let bg = NSView(frame: panel.contentView!.bounds)
        bg.autoresizingMask = [.width, .height]
        bg.wantsLayer = true
        bg.layer?.backgroundColor = NSColor(white: 0.04, alpha: 0.94).cgColor
        bg.layer?.cornerRadius = 14
        bg.layer?.borderWidth = 1
        bg.layer?.borderColor = NSColor(white: 1, alpha: 0.14).cgColor
        panel.contentView = bg

        label.font = .systemFont(ofSize: 13)
        label.textColor = NSColor(white: 1, alpha: 0.9)
        label.maximumNumberOfLines = 12
        label.lineBreakMode = .byTruncatingTail
        bg.addSubview(label)

        button.bezelStyle = .rounded
        button.controlSize = .regular
        button.target = self
        button.action = #selector(copyClicked)
        bg.addSubview(button)
    }

    func show(_ text: String, seconds: Double = 10) {
        self.text = text
        label.stringValue = text
        button.title = "Copy"
        let width: CGFloat = text.count > 120 ? 560 : 440
        let fit = (text as NSString).boundingRect(with: NSSize(width: width - 110, height: 400),
                                                  options: [.usesLineFragmentOrigin], attributes: [.font: label.font!])
        let height: CGFloat = max(64, min(240, ceil(fit.height) + 26))
        let screen = NSScreen.screens.first { NSMouseInRect(NSEvent.mouseLocation, $0.frame, false) } ?? NSScreen.main!
        let f = screen.visibleFrame
        panel.setFrame(NSRect(x: f.midX - width / 2, y: f.minY + 22, width: width, height: height), display: true)
        label.frame = NSRect(x: 16, y: 10, width: width - 110, height: height - 20)
        button.frame = NSRect(x: width - 86, y: (height - 30) / 2, width: 72, height: 30)
        panel.alphaValue = 1
        panel.orderFrontRegardless()
        scheduleHide(after: seconds)
    }

    @objc private func copyClicked() {
        copyToClipboard(text)
        button.title = "Copied"
        scheduleHide(after: 0.9)
    }

    private func scheduleHide(after seconds: Double) {
        hideWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.panel.orderOut(nil) }
        hideWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: work)
    }
}

// MARK: - Menu bar icon: a small arc reactor (template image, so macOS tints it for light/dark)

func reactorIcon(listening: Bool) -> NSImage {
    let size = NSSize(width: 18, height: 18)
    let image = NSImage(size: size, flipped: false) { rect in
        NSColor.black.setStroke()
        NSColor.black.setFill()
        let c = NSPoint(x: rect.midX, y: rect.midY)
        // outer ring
        let outer = NSBezierPath(ovalIn: rect.insetBy(dx: 1, dy: 1))
        outer.lineWidth = 1.3
        outer.stroke()
        // segmented middle ring (8 segments, like the reactor's coils)
        for i in 0..<8 {
            let start = CGFloat(i) * 45 + 8
            let seg = NSBezierPath()
            seg.appendArc(withCenter: c, radius: 5.4, startAngle: start, endAngle: start + 29)
            seg.lineWidth = 2.1
            seg.lineCapStyle = .butt
            seg.stroke()
        }
        // core: hollow when idle, solid while listening
        let core = NSBezierPath(ovalIn: NSRect(x: c.x - 2.4, y: c.y - 2.4, width: 4.8, height: 4.8))
        if listening { core.fill() } else { core.lineWidth = 1.1; core.stroke() }
        return true
    }
    image.isTemplate = true
    image.accessibilityDescription = listening ? "Jarvis is listening" : "Jarvis"
    return image
}

// MARK: - App

final class App: NSObject, NSApplicationDelegate {
    let recorder = Recorder()
    let bubble = Bubble()
    let toast = CopyToast()
    var recent: [String] = []
    var status: NSStatusItem!
    var tap: CFMachPort?

    var fnDown = false
    var fnPressedAt = Date.distantPast
    var lastTapAt = Date.distantPast
    var comboKeyPressed = false
    var handsFree = false
    var handsFreeStartedOnThisPress = false
    var busy = false
    var pendingTapCancel: DispatchWorkItem?
    var handsFreeStartedAt = Date.distantPast
    var stoppedAt = Date.distantPast
    var ignoringThisPress = false
    var target: (element: AXUIElement, app: NSRunningApplication?)?

    // Asking JARVIS (Right Option)
    let speaker = Speaker()
    var askDown = false
    var askPressedAt = Date.distantPast
    var askCombo = false
    var asking = false          // this recording is a question, not dictation
    var askBusy = false         // waiting for Whisper or JARVIS
    var lastAskTapAt = Date.distantPast
    var conversing = false      // JARVIS just answered: listen for a reply once it stops speaking
    var awaitingConfirm = false // its answer was "shall I?": "no" or "never mind" must reach it
    var listeningHandsFree = false   // the mic is open waiting for you to speak (no key held)
    var vadTimer: Timer?
    var screen: [String: String] = [:]   // what was in front of you when you started asking
    var followUpOn = UserDefaults.standard.object(forKey: "followUp") as? Bool ?? true

    /// Remember the text box you're in, before fn taps open the emoji picker.
    func captureTarget() {
        if let el = focusedElement(), isEditable(el) {
            target = (el, NSWorkspace.shared.frontmostApplication)
        } else {
            target = nil
        }
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        status = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        setIcon(recording: false)
        rebuildMenu()
        log("Jarvis started (Accessibility trusted: \(AXIsProcessTrusted()))")
        // Wake each app's accessibility as it comes to the front, so the first dictation into it pastes.
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main) { note in
            wakeAccessibility(of: note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication)
        }
        wakeAccessibility(of: NSWorkspace.shared.frontmostApplication)
        takeOverFnKey()
        // `pkill`/logout send SIGTERM: give fn back before exiting
        signal(SIGTERM, SIG_IGN)
        let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        term.setEventHandler { NSApp.terminate(nil) }
        term.resume()
        sigterm = term
        bubble.levelSource = { [weak self] in self?.recorder.level ?? 0 }

        AVCaptureDevice.requestAccess(for: .audio) { _ in }
        _ = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
        if !CGPreflightListenEventAccess() { CGRequestListenEventAccess() }
        installTap()
        speaker.preload(acknowledgements)
        Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in
            self?.speaker.preload(acknowledgements)
        }
        startAnnouncements()
        speaker.onIdle = { [weak self] in self?.speakerIdle() }
    }

    var sigterm: DispatchSourceSignal?

    func applicationWillTerminate(_ note: Notification) {
        releaseFnKey()
    }

    func rebuildMenu() {
        let menu = NSMenu()
        menu.addItem(withTitle: "Hold fn to dictate · double-tap for hands-free", action: nil, keyEquivalent: "")
        menu.addItem(withTitle: "Hold Right Option to ask JARVIS · double-tap to just talk", action: nil, keyEquivalent: "")
        let followUp = NSMenuItem(title: "Keep listening after answers", action: #selector(toggleFollowUp(_:)), keyEquivalent: "")
        followUp.target = self
        followUp.state = followUpOn ? .on : .off
        menu.addItem(followUp)
        let privacy = NSMenuItem(title: "Privacy mode (TV)", action: #selector(togglePrivacy(_:)), keyEquivalent: "")
        privacy.target = self
        privacy.state = privacyOn ? .on : .off
        menu.addItem(privacy)
        let announce = NSMenuItem(title: "Announcements (sessions, meetings)", action: #selector(toggleAnnouncements(_:)), keyEquivalent: "")
        announce.target = self
        announce.state = announceOn ? .on : .off
        menu.addItem(announce)
        menu.addItem(.separator())
        if recent.isEmpty {
            menu.addItem(withTitle: "No transcripts yet", action: nil, keyEquivalent: "")
        } else {
            menu.addItem(withTitle: "Recent (click to copy)", action: nil, keyEquivalent: "")
            for text in recent {
                let title = text.count > 60 ? String(text.prefix(60)) + "…" : text
                let item = NSMenuItem(title: title, action: #selector(copyRecent(_:)), keyEquivalent: "")
                item.target = self
                item.representedObject = text
                menu.addItem(item)
            }
        }
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit Jarvis", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        status.menu = menu
    }

    var privacyOn = false

    @objc func toggleFollowUp(_ item: NSMenuItem) {
        followUpOn.toggle()
        UserDefaults.standard.set(followUpOn, forKey: "followUp")
        if !followUpOn { conversing = false; endHandsFree() }
        rebuildMenu()
    }

    @objc func togglePrivacy(_ item: NSMenuItem) {
        let url = jarvisURL("/api/privacy", ["on": privacyOn ? "0" : "1"])
        URLSession.shared.dataTask(with: url) { data, _, _ in
            let json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async {
                if let on = json?["privacy"] as? Bool { self.privacyOn = on; self.rebuildMenu() }
            }
        }.resume()
    }

    // MARK: Announcements: JARVIS speaking up (see tools/announce.py)

    var announceOn = true
    var announceAfter: Int?          // last announcement handled; nil until the first poll
    var announceTimer: Timer?

    @objc func toggleAnnouncements(_ item: NSMenuItem) {
        URLSession.shared.dataTask(with: jarvisURL("/api/announce", ["on": announceOn ? "0" : "1"])) { data, _, _ in
            let json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async {
                if let on = json?["enabled"] as? Bool { self.announceOn = on; self.rebuildMenu() }
            }
        }.resume()
    }

    func startAnnouncements() {
        announceTimer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in self?.pollAnnouncements() }
        pollAnnouncements()
    }

    func pollAnnouncements() {
        var params: [String: String] = [:]
        if let after = announceAfter { params["after"] = String(after) }
        URLSession.shared.dataTask(with: jarvisURL("/api/announce", params)) { data, _, _ in
            guard let json = data.flatMap({ try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }) else { return }
            DispatchQueue.main.async { self.gotAnnouncements(json) }
        }.resume()
    }

    func gotAnnouncements(_ json: [String: Any]) {
        if let on = json["enabled"] as? Bool, on != announceOn { announceOn = on; rebuildMenu() }
        let latest = json["latest"] as? Int ?? 0
        guard let after = announceAfter, latest >= after else { announceAfter = latest; return }  // first poll, or server restarted
        let items = json["items"] as? [[String: Any]] ?? []
        guard !items.isEmpty else { return }
        // Not while you're talking or waiting on an answer; the next poll brings them again.
        if recorder.running || busy || askBusy || fnDown || askDown { return }
        let texts = items.compactMap { $0["text"] as? String }
        let text = texts.joined(separator: " ")
        log("  announce: \"\(text.prefix(80))\"")
        toast.show(text, seconds: max(10, Double(text.count) / 10))
        for item in items where item["speak"] as? Bool ?? true {
            if let t = item["text"] as? String { speaker.say(t) }
        }
        // News from the session JARVIS is following: listen for an answer ("tell it to go ahead")
        if items.contains(where: { ($0["reply"] as? Bool ?? false) && ($0["speak"] as? Bool ?? true) }) {
            conversing = followUpOn
            awaitingConfirm = false
        }
        announceAfter = items.compactMap { $0["id"] as? Int }.max() ?? latest
    }

    @objc func copyRecent(_ item: NSMenuItem) {
        if let text = item.representedObject as? String { copyToClipboard(text) }
    }

    func setIcon(recording: Bool) {
        status.button?.image = reactorIcon(listening: recording)
    }

    // Retry until Input Monitoring is granted; the tap can't be created before that.
    func installTap() {
        // 14 = NSSystemDefined: special keys (media, Globe/fn) also send one of these
        let mask = (1 << CGEventType.flagsChanged.rawValue) | (1 << CGEventType.keyDown.rawValue)
                 | (1 << CGEventType.keyUp.rawValue) | (1 << 14)
        let me = Unmanaged.passUnretained(self).toOpaque()
        // An active tap (like Wispr Flow): we swallow the fn key's own press/release so macOS
        // never sees it and never opens the emoji picker. fn+key combos are unaffected.
        // HID level: the earliest point, before macOS acts on the Globe/fn key (emoji picker).
        tap = CGEvent.tapCreate(tap: .cghidEventTap, place: .headInsertEventTap, options: .defaultTap,
                                eventsOfInterest: CGEventMask(mask), callback: { _, type, event, ref in
            let app = Unmanaged<App>.fromOpaque(ref!).takeUnretainedValue()
            if type.rawValue == 14 {
                if let ns = NSEvent(cgEvent: event) {
                    log("  sysdefined subtype=\(ns.subtype.rawValue) data1=0x\(String(ns.data1, radix: 16)) data2=0x\(String(ns.data2, radix: 16))")
                }
                return Unmanaged.passUnretained(event)
            }
            app.handle(type, event)
            let code = event.getIntegerValueField(.keyboardEventKeycode)
            if type == .flagsChanged && code == fnKeyCode { return nil }
            if (type == .keyDown || type == .keyUp) && code == f18KeyCode { return nil }  // our remapped fn
            return Unmanaged.passUnretained(event)
        }, userInfo: me)
        guard let tap else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.installTap() }
            return
        }
        log("event tap installed at HID level")
        let source = CFMachPortCreateRunLoopSource(nil, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
    }

    func handle(_ type: CGEventType, _ event: CGEvent) {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
            return
        }
        let code = event.getIntegerValueField(.keyboardEventKeycode)
        if code == f18KeyCode && (type == .keyDown || type == .keyUp) {
            // The physical fn key, remapped. Ignore key-repeat while it's held.
            if type == .keyDown && event.getIntegerValueField(.keyboardEventAutorepeat) != 0 { return }
            let down = type == .keyDown
            log("fn(F18) \(down ? "down" : "up")")
            DispatchQueue.main.async { down ? self.fnPressed() : self.fnReleased() }
            return
        }
        if type == .keyUp { return }
        if type == .keyDown {
            if event.getIntegerValueField(.eventSourceUserData) == syntheticMarker { return }
            let key = event.getIntegerValueField(.keyboardEventKeycode)
            DispatchQueue.main.async {
                if self.askDown {                 // Option+key: typing a character, not asking
                    self.askCombo = true
                    if self.asking { self.cancel() }
                    return
                }
                if key == escapeKeyCode && self.listeningHandsFree {
                    self.endHandsFree()        // Esc: stop listening for a reply
                    return
                }
                if key == escapeKeyCode && self.speaker.speaking {
                    self.conversing = false
                    self.speaker.stop()
                    self.bubble.hide()
                    return
                }
                if key == escapeKeyCode && (self.handsFree || self.recorder.running) {
                    self.handsFree = false     // Esc: throw the recording away
                    self.cancel()
                } else if self.fnDown {
                    self.comboKeyPressed = true
                    if !self.handsFree { self.cancel() }
                }
            }
            return
        }
        if type == .flagsChanged && code == rightOptionKeyCode {
            let down = event.flags.rawValue & rightOptionMask != 0
            DispatchQueue.main.async { down ? self.askPressed() : self.askReleased() }
            return
        }
        guard type == .flagsChanged, event.getIntegerValueField(.keyboardEventKeycode) == fnKeyCode else { return }
        let down = event.flags.contains(.maskSecondaryFn)
        log("fn \(down ? "down" : "up")  flags=\(String(event.flags.rawValue, radix: 16))")
        DispatchQueue.main.async { down ? self.fnPressed() : self.fnReleased() }
    }

    func fnPressed() {
        guard !fnDown else { return }
        fnDown = true
        comboKeyPressed = false
        fnPressedAt = Date()
        handsFreeStartedOnThisPress = false
        ignoringThisPress = false
        if listeningHandsFree { endHandsFree() }           // waiting for a reply: dictation wins
        if asking || askBusy {                            // JARVIS has the mic: leave it alone
            ignoringThisPress = true
            return
        }
        if handsFree { log("  press while hands-free: will finish on release"); return }
        if Date().timeIntervalSince(stoppedAt) < afterStopIgnore {
            log("  press right after stopping: ignored (second tap of a double-tap stop)")
            ignoringThisPress = true
            return
        }
        let sinceTap = Date().timeIntervalSince(lastTapAt)
        log(String(format: "  pressed; %.2fs since last tap (window %.2fs)", sinceTap, doubleTapWindow))
        if sinceTap < doubleTapWindow {
            // Second tap of a double-tap: start hands-free right away, like Wispr Flow.
            lastTapAt = .distantPast
            handsFree = true
            handsFreeStartedOnThisPress = true
            handsFreeStartedAt = Date()
            log("  -> DOUBLE-TAP: hands-free on (mic \(recorder.running ? "still warm" : "restarting"))")
            pendingTapCancel?.cancel()
            pendingTapCancel = nil
            if !recorder.running { startRecording() }   // normally still running from the first tap
            bubble.show(.handsFree)
            for delay in [0.3, 0.7] {                    // the picker appears a moment after the tap
                DispatchQueue.main.asyncAfter(deadline: .now() + delay) { dismissEmojiPicker() }
            }
            return
        }
        pendingTapCancel?.cancel()
        pendingTapCancel = nil
        if recorder.running { recorder.restartTake(); captureTarget() } else { startRecording() }   // push-to-talk while held
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
            if self.recorder.running && !self.handsFree && self.fnDown { self.bubble.show(.listening) }
        }
    }

    func fnReleased() {
        guard fnDown else { return }
        fnDown = false
        let held = Date().timeIntervalSince(fnPressedAt)
        log(String(format: "  released after %.2fs (handsFree=%@, combo=%@)", held,
                   handsFree ? "yes" : "no", comboKeyPressed ? "yes" : "no"))
        if ignoringThisPress { return }
        if handsFree {
            if handsFreeStartedOnThisPress { return }  // keep listening
            if Date().timeIntervalSince(handsFreeStartedAt) < handsFreeGrace {
                log("  -> stray tap during hands-free start: still listening")
                return
            }
            if !comboKeyPressed { handsFree = false; stoppedAt = Date(); finish() }
            return
        }
        if comboKeyPressed { return cancel() }
        if held >= minHold { return finish() }
        lastTapAt = Date()                             // a quick tap: maybe the first of a double-tap
        log("  -> tap (mic kept warm for a second tap)")
        // Keep the mic running briefly: restarting it for the second tap loses the first
        // second of speech on many mics (AirPods especially).
        pendingTapCancel?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, !self.handsFree, !self.fnDown else { return }
            self.cancel()
        }
        pendingTapCancel = work
        DispatchQueue.main.asyncAfter(deadline: .now() + doubleTapWindow, execute: work)
    }

    // MARK: Ask JARVIS (hold Right Option)

    func askPressed() {
        guard !askDown else { return }
        askDown = true
        askCombo = false
        askPressedAt = Date()
        speaker.stop()                                    // talking over JARVIS interrupts it
        if listeningHandsFree { endHandsFree() }          // holding the key instead: push-to-talk as usual
        guard !askBusy, !busy, !recorder.running, !fnDown else { return }
        asking = true
        bubble.asking = true
        do {
            try recorder.start()
            setIcon(recording: true)
        } catch {
            log("  mic failed to start: \(error)")
            asking = false
            return
        }
        screen = screenContext()   // after the mic is on: some apps are slow to answer
        DispatchQueue.main.asyncAfter(deadline: .now() + askMinHold) {
            if self.asking && self.askDown && !self.askCombo && self.recorder.running { self.bubble.show(.listening) }
        }
    }

    func askReleased() {
        guard askDown else { return }
        askDown = false
        guard asking else { return }
        let held = Date().timeIntervalSince(askPressedAt)
        if askCombo { asking = false; return cancel() }
        if held < askMinHold {
            if Date().timeIntervalSince(lastAskTapAt) < doubleTapWindow {   // double-tap: ask without holding
                lastAskTapAt = .distantPast
                log("ask: double-tap, listening hands-free")
                return listenHandsFree(wait: handsFreeAskWait)
            }
            lastAskTapAt = Date()
            asking = false
            return cancel()
        }
        let peak = recorder.peak
        let (wav, seconds) = recorder.stop()
        setIcon(recording: false)
        submitQuestion(wav, seconds, peak, minPeak: minSpeechLevel)
    }

    /// A recorded question: transcribe it and ask JARVIS (or end the conversation on "that's all").
    func submitQuestion(_ wav: Data, _ seconds: Double, _ peak: Float, minPeak: Float) {
        log(String(format: "ask: %.1fs audio, peak %.3f", seconds, peak))
        guard seconds >= 0.4, peak >= minPeak else { asking = false; conversing = false; bubble.asking = false; return bubble.hide() }
        askBusy = true
        bubble.show(.transcribing)
        transcribe(wav) { result in
            DispatchQueue.main.async {
                guard case .success(let text) = result, !text.isEmpty, !isHallucination(text, seconds: seconds) else {
                    self.askBusy = false
                    self.asking = false
                    self.conversing = false
                    self.bubble.asking = false
                    self.bubble.hide()
                    if case .failure = result { NSSound.beep() }
                    return
                }
                if !self.awaitingConfirm && isConversationEnd(text) {
                    log("  ask -> \"\(text.prefix(40))\": that's all, conversation over")
                    self.askBusy = false
                    self.asking = false
                    self.conversing = false
                    self.bubble.asking = false
                    self.bubble.hide()
                    return
                }
                self.sendQuestion(text)
            }
        }
    }

    func sendQuestion(_ text: String) {
        log("  ask -> \"\(text.prefix(80))\"")
        bubble.show(.thinking)
        conversing = false
        var spoke = false   // said anything yet: what it's doing ("Checking your calendar."), or the answer
        DispatchQueue.main.asyncAfter(deadline: .now() + ackDelay) {   // still nothing at all: a stock line
            if !spoke && self.askBusy {
                spoke = true
                self.speaker.say(acknowledgements.randomElement()!)
            }
        }
        if let app = screen["app"] { log("  screen: \(app)\(screen["selected"] != nil ? " (with selected text)" : "")") }
        askJarvis(text, screen: screen, onEvent: { type, line in
            if type == "status" {
                log("  status: \(line)")
                if !spoke { spoke = true; self.speaker.say(line) }
            } else if type == "say" {
                if !self.speaker.busy { self.bubble.hide() }
                spoke = true
                self.speaker.say(line)
            }
        }, completion: { answer in
            spoke = true
            self.askBusy = false
            self.asking = false
            self.bubble.asking = false
            guard let answer, !(answer.reply.isEmpty && answer.speak.isEmpty) else {
                self.bubble.hide()
                self.conversing = false
                self.toast.show("JARVIS isn't answering. Is the JARVIS server running?")
                return
            }
            let reply = answer.reply
            log("  reply <- \"\(reply.prefix(80))\"")
            self.bubble.hide()
            self.awaitingConfirm = answer.details != nil
            if let details = answer.details {   // an action: show exactly what will happen, until it's answered
                self.toast.show("\(reply)\n\n\(details)\n\nSay \u{201C}go ahead\u{201D} to do it, or \u{201C}cancel\u{201D}.",
                                seconds: 90)
            } else {
                self.toast.show(reply, seconds: max(8, Double(reply.count) / 12))
            }
            self.conversing = self.followUpOn
            if !answer.speak.isEmpty { self.speaker.say(answer.speak) } else if !self.speaker.busy { self.speakerIdle() }
        })
    }

    /// JARVIS stopped talking. If it just answered you, listen a moment for a reply.
    func speakerIdle() {
        guard !askBusy, !recorder.running else { return }
        bubble.hide()
        guard conversing, followUpOn else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {   // let the last word die away first
            guard self.conversing, !self.speaker.busy, !self.askBusy, !self.recorder.running,
                  !self.busy, !self.fnDown, !self.askDown else { return }
            self.listenHandsFree(wait: followUpWait, minPeak: handsFreeMinPeak)
        }
    }

    /// Listen with no key held, for a reply or after a double-tap: ends itself when you pause,
    /// or after `wait` if you don't start speaking.
    func listenHandsFree(wait: TimeInterval, minPeak: Float = minSpeechLevel) {
        if !recorder.running {
            do { try recorder.start() } catch {
                log("  mic failed to start: \(error)")
                conversing = false
                return
            }
        }
        asking = true
        bubble.asking = true
        listeningHandsFree = true
        screen = screenContext()
        setIcon(recording: true)
        bubble.show(.listening)
        let started = Date()
        var room: Float = 0, roomTicks: Float = 0, loudTicks = 0
        var speaking = false, lastLoud = started
        vadTimer?.invalidate()
        vadTimer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] timer in
            guard let self, self.listeningHandsFree, self.recorder.running else { return timer.invalidate() }
            let level = self.recorder.level, now = Date()
            if now.timeIntervalSince(started) < 0.25 { room += level; roomTicks += 1; return }   // the room's own hum
            let noise = min(roomTicks > 0 ? room / roomTicks : 0, 0.01)   // capped: you may have started straight away
            if !speaking {
                loudTicks = level > max(0.012, noise * 3) ? loudTicks + 1 : 0
                if loudTicks >= 3 {
                    speaking = true
                    lastLoud = now
                    log(String(format: "  hands-free: speech started (room %.4f)", noise))
                } else if now.timeIntervalSince(started) > wait {
                    log("  hands-free: nothing said")
                    self.endHandsFree()
                }
                return
            }
            if level > max(0.006, noise * 2) { lastLoud = now }
            guard now.timeIntervalSince(lastLoud) > endOfSpeechPause
                    || now.timeIntervalSince(started) > maxQuestionSeconds else { return }
            timer.invalidate()
            self.vadTimer = nil
            self.listeningHandsFree = false
            let peak = self.recorder.peak
            let (wav, seconds) = self.recorder.stop()
            self.setIcon(recording: false)
            self.submitQuestion(wav, seconds, peak, minPeak: minPeak)
        }
    }

    /// Stop waiting for you to speak; the conversation is over until the next question.
    func endHandsFree() {
        vadTimer?.invalidate()
        vadTimer = nil
        conversing = false
        guard listeningHandsFree else { return }
        listeningHandsFree = false
        if recorder.running { _ = recorder.stop() }
        asking = false
        bubble.asking = false
        setIcon(recording: false)
        bubble.hide()
    }

    func startRecording() {
        guard !busy, !recorder.running else { return }
        captureTarget()
        do {
            try recorder.start()
            setIcon(recording: true)
            // Only show the bubble once it's clearly a hold, so taps don't flash it.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                if self.recorder.running && !self.handsFree && self.fnDown { self.bubble.show(.listening) }
            }
        } catch {
            log("  mic failed to start: \(error)")
            NSSound.beep()
        }
    }

    func cancel() {
        if recorder.running { _ = recorder.stop() }
        if !askBusy { asking = false; bubble.asking = false }
        setIcon(recording: false)
        if !busy { bubble.hide() }
    }

    func finish() {
        guard recorder.running else { return }
        let peak = recorder.peak
        let (wav, seconds) = recorder.stop()
        setIcon(recording: false)
        // No real speech: Whisper would invent "Thank you." or similar for silence.
        log(String(format: "  finish: %.1fs audio, peak %.3f", seconds, peak))
        guard seconds >= 0.4, peak >= minSpeechLevel else { log("  -> no speech, skipped"); return bubble.hide() }
        busy = true
        bubble.show(.transcribing)
        transcribe(wav) { result in
            DispatchQueue.main.async {
                self.busy = false
                switch result {
                case .success(let text) where !text.isEmpty && !isHallucination(text, seconds: seconds):
                    self.bubble.hide()
                    saveToHistory(text)
                    self.recent.insert(text, at: 0)
                    self.recent = Array(self.recent.prefix(10))
                    self.rebuildMenu()
                    let pickerWasOpen = emojiPickerOpen()
                    let deliver = {
                        let (editable, role) = focusedElementIsEditable()
                        log("  -> \"\(text.prefix(40))\" focus=\(role) editable=\(editable) picker=\(pickerWasOpen)")
                        // After the picker, "focus" may be its search box or the bare window: trust
                        // the text box you started in instead.
                        if editable && !pickerWasOpen { return paste(text) }
                        if pickerWasOpen && self.target == nil && editable { return paste(text) }
                        guard let t = self.target, isEditable(t.element) else { return self.toast.show(text) }
                        // Focus got lost (e.g. after the emoji picker): put the cursor back and paste there.
                        log("  -> returning focus to the text box you started in")
                        t.app?.activate()
                        AXUIElementSetAttributeValue(t.element, kAXFocusedAttribute as CFString, kCFBooleanTrue)
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { paste(text) }
                    }
                    // If the stop-tap opened the emoji picker, close it (and wait until it's really
                    // gone) so the text lands in your message rather than the emoji search box.
                    closeEmojiPicker(then: deliver)
                case .success:
                    self.bubble.hide()
                case .failure:
                    self.bubble.hide()
                    NSSound.beep()   // Whisper engine not running
                }
            }
        }
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
