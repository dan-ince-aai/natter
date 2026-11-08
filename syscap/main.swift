// syscap: capture everything the Mac is playing (a Core Audio process tap, macOS 14.4+), minus the
// processes passed with --exclude-pid, and write it to stdout as raw float32 mono little-endian.
// The first line on stderr is "RATE <hz>". Stops on SIGTERM/SIGINT or when stdout closes.
import AudioToolbox
import CoreAudio
import Foundation

signal(SIGPIPE, SIG_IGN)

func log(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }

var excludePIDs: [pid_t] = []
var useClock = true
var muteOriginal = false
var printClockOnly = false  // --print-clock: report the clock device and exit, creating nothing
var argv = Array(CommandLine.arguments.dropFirst())
while !argv.isEmpty {
    let a = argv.removeFirst()
    if a == "--exclude-pid", let v = argv.first, let p = pid_t(v) { excludePIDs.append(p); argv.removeFirst() }
    if a == "--no-clock" { useClock = false }
    if a == "--mute" { muteOriginal = true }
    if a == "--print-clock" { printClockOnly = true }
}

func processObject(_ pid: pid_t) -> AudioObjectID? {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
                                          mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var p = pid
    var obj = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let st = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr,
                                        UInt32(MemoryLayout<pid_t>.size), &p, &size, &obj)
    return (st == noErr && obj != kAudioObjectUnknown) ? obj : nil
}

func streamCount(_ dev: AudioDeviceID, _ scope: AudioObjectPropertyScope) -> Int {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams, mScope: scope,
                                          mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(dev, &addr, 0, nil, &size) == noErr else { return 0 }
    return Int(size) / MemoryLayout<AudioStreamID>.size
}

func deviceUID(_ dev: AudioDeviceID) -> String? {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyDeviceUID, mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var uid: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &uid) == noErr, let u = uid else { return nil }
    return u.takeRetainedValue() as String
}

/// The tap needs a real device in the aggregate to clock it. Use one with no microphone, so a Bluetooth headset
/// (AirPods) is never opened for input, which would switch it to low-quality call audio.
func transportType(_ dev: AudioDeviceID) -> UInt32 {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyTransportType,
                                          mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var t: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &t)
    return t
}

/// The aggregate needs a real device as its clock (a tap-only aggregate delivers silence). It must never be a
/// Bluetooth device: wrapping the AirPods in our aggregate while the translation also plays to them wedges
/// Bluetooth audio until coreaudiod restarts. Nothing is played through the clock device; it only supplies timing.
/// Preference: built-in speakers, then any other wired/USB/HDMI output without input streams (no BlackHole,
/// no aggregates, no virtual devices).
func clockDeviceUID() -> String? {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
                                          mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size) == noErr else { return nil }
    var devs = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &devs)
    let avoid: Set<UInt32> = [kAudioDeviceTransportTypeBluetooth, kAudioDeviceTransportTypeBluetoothLE,
                              kAudioDeviceTransportTypeAggregate, kAudioDeviceTransportTypeAutoAggregate,
                              kAudioDeviceTransportTypeVirtual, kAudioDeviceTransportTypeAirPlay, kAudioDeviceTransportTypeContinuityCaptureWired,
                              kAudioDeviceTransportTypeContinuityCaptureWireless]
    let outs = devs.filter { streamCount($0, kAudioObjectPropertyScopeOutput) > 0 && !avoid.contains(transportType($0)) &&
                             !(deviceUID($0) ?? "").lowercased().contains("blackhole") }
    if let builtIn = outs.first(where: { transportType($0) == kAudioDeviceTransportTypeBuiltIn }) { return deviceUID(builtIn) }
    if let wired = outs.first(where: { streamCount($0, kAudioObjectPropertyScopeInput) == 0 }) { return deviceUID(wired) }
    return outs.first.flatMap { deviceUID($0) }
}

func defaultOutputUID() -> String? {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                          mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var dev = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &dev) == noErr else { return nil }
    addr.mSelector = kAudioDevicePropertyDeviceUID
    var uid: Unmanaged<CFString>?
    size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &uid) == noErr, let u = uid else { return nil }
    return u.takeRetainedValue() as String
}

if printClockOnly {
    print("CLOCK", clockDeviceUID() ?? "none")
    exit(0)
}

var excluded: [AudioObjectID] = []
for pid in excludePIDs {
    if let o = processObject(pid) { excluded.append(o) } else { log("WARN no audio process object for pid \(pid) yet") }
}

let tapDesc = CATapDescription(stereoGlobalTapButExcludeProcesses: excluded)
tapDesc.uuid = UUID()
tapDesc.muteBehavior = muteOriginal ? .mutedWhenTapped : .unmuted  // --mute: you hear only the translation
tapDesc.isPrivate = true
tapDesc.name = "live-interpreter"
var tapID = AudioObjectID(kAudioObjectUnknown)
var st = AudioHardwareCreateProcessTap(tapDesc, &tapID)
guard st == noErr else { log("ERROR AudioHardwareCreateProcessTap \(st)"); exit(2) }

var fmtAddr = AudioObjectPropertyAddress(mSelector: kAudioTapPropertyFormat, mScope: kAudioObjectPropertyScopeGlobal,
                                         mElement: kAudioObjectPropertyElementMain)
var fmt = AudioStreamBasicDescription()
var fmtSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
st = AudioObjectGetPropertyData(tapID, &fmtAddr, 0, nil, &fmtSize, &fmt)
guard st == noErr else { log("ERROR tap format \(st)"); exit(2) }

var agg: [String: Any] = [
    kAudioAggregateDeviceNameKey: "Live Interpreter Tap",
    kAudioAggregateDeviceUIDKey: UUID().uuidString,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceIsStackedKey: false,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true, kAudioSubTapUIDKey: tapDesc.uuid.uuidString]],
]
if useClock, let out = clockDeviceUID() {
    log("CLOCK \(out)")
    agg[kAudioAggregateDeviceMainSubDeviceKey] = out
    agg[kAudioAggregateDeviceSubDeviceListKey] = [[kAudioSubDeviceUIDKey: out]]
}
var aggID = AudioObjectID(kAudioObjectUnknown)
st = AudioHardwareCreateAggregateDevice(agg as CFDictionary, &aggID)
guard st == noErr else { log("ERROR AudioHardwareCreateAggregateDevice \(st)"); AudioHardwareDestroyProcessTap(tapID); exit(2) }

func cleanup() {
    AudioHardwareDestroyAggregateDevice(aggID)
    AudioHardwareDestroyProcessTap(tapID)
}

let isFloat = fmt.mFormatFlags & kAudioFormatFlagIsFloat != 0
let nonInterleaved = fmt.mFormatFlags & kAudioFormatFlagIsNonInterleaved != 0
let channels = max(1, Int(fmt.mChannelsPerFrame))
log("RATE \(Int(fmt.mSampleRate))")
log("FORMAT float=\(isFloat) noninterleaved=\(nonInterleaved) channels=\(channels) bits=\(fmt.mBitsPerChannel) excluded=\(excluded.count)")
guard isFloat, fmt.mBitsPerChannel == 32 else { log("ERROR unsupported tap format"); cleanup(); exit(2) }

var mono = [Float](repeating: 0, count: 8192)
let queue = DispatchQueue(label: "syscap", qos: .userInteractive)
var procID: AudioDeviceIOProcID?
st = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, queue) { _, inInput, _, _, _ in
    let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inInput))
    guard abl.count > 0, let d0 = abl[0].mData else { return }
    var frames: Int
    if nonInterleaved {
        frames = Int(abl[0].mDataByteSize) / 4
        if mono.count < frames { mono = [Float](repeating: 0, count: frames) }
        let n = Float(abl.count)
        for i in 0..<frames { mono[i] = 0 }
        for b in abl {
            guard let p = b.mData?.assumingMemoryBound(to: Float.self) else { continue }
            for i in 0..<frames { mono[i] += p[i] / n }
        }
    } else {
        frames = Int(abl[0].mDataByteSize) / (4 * channels)
        if mono.count < frames { mono = [Float](repeating: 0, count: frames) }
        let p = d0.assumingMemoryBound(to: Float.self)
        for i in 0..<frames {
            var s: Float = 0
            for c in 0..<channels { s += p[i * channels + c] }
            mono[i] = s / Float(channels)
        }
    }
    let ok = mono.withUnsafeBytes { raw -> Bool in
        var off = 0
        let total = frames * 4
        while off < total {
            let w = write(1, raw.baseAddress! + off, total - off)
            if w <= 0 { return false }
            off += w
        }
        return true
    }
    if !ok { cleanup(); exit(0) }
}
guard st == noErr, let pid = procID else { log("ERROR AudioDeviceCreateIOProcIDWithBlock \(st)"); cleanup(); exit(2) }
st = AudioDeviceStart(aggID, pid)
guard st == noErr else { log("ERROR AudioDeviceStart \(st)"); cleanup(); exit(2) }
log("STARTED")

for sig in [SIGINT, SIGTERM] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { AudioDeviceStop(aggID, pid); AudioDeviceDestroyIOProcID(aggID, pid); cleanup(); exit(0) }
    src.resume()
    _ = Unmanaged.passRetained(src as AnyObject)
}
dispatchMain()
