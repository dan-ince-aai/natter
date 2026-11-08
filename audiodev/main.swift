// audiodev: read or set the Mac's default input or output device.
//   audiodev get-input | get-output          -> prints the default device's name
//   audiodev set-input | set-output <name>   -> makes the device with that exact name (or substring) the default
import CoreAudio
import Foundation

func prop<T>(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal, _ value: inout T) -> Bool {
    var addr = AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<T>.size)
    return AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &value) == noErr
}

func name(_ dev: AudioDeviceID) -> String {
    var n: Unmanaged<CFString>?
    return prop(dev, kAudioObjectPropertyName, kAudioObjectPropertyScopeGlobal, &n) ? (n!.takeRetainedValue() as String) : ""
}

func inputStreams(_ dev: AudioDeviceID) -> Int { streams(dev, kAudioObjectPropertyScopeInput) }

func streams(_ dev: AudioDeviceID, _ scope: AudioObjectPropertyScope) -> Int {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams, mScope: scope,
                                          mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    return AudioObjectGetPropertyDataSize(dev, &addr, 0, nil, &size) == noErr ? Int(size) / MemoryLayout<AudioStreamID>.size : 0
}

func devices() -> [AudioDeviceID] {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices, mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size)
    var devs = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &devs)
    return devs
}

let args = CommandLine.arguments
let system = AudioObjectID(kAudioObjectSystemObject)

func getDefault(_ sel: AudioObjectPropertySelector) {
    var dev = AudioDeviceID(0)
    guard prop(system, sel, kAudioObjectPropertyScopeGlobal, &dev) else { exit(1) }
    print(name(dev))
}

func setDefault(_ sel: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope, _ want: String) {
    let cands = devices().filter { streams($0, scope) > 0 }
    guard var dev = cands.first(where: { name($0) == want }) ?? cands.first(where: { name($0).lowercased().contains(want.lowercased()) }) else {
        FileHandle.standardError.write("no device named \(want)\n".data(using: .utf8)!); exit(1)
    }
    var addr = AudioObjectPropertyAddress(mSelector: sel, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    let st = AudioObjectSetPropertyData(system, &addr, 0, nil, UInt32(MemoryLayout<AudioDeviceID>.size), &dev)
    if st != noErr { FileHandle.standardError.write("set failed \(st)\n".data(using: .utf8)!); exit(1) }
    print(name(dev))
}

switch (args.count >= 2 ? args[1] : "", args.count >= 3 ? args[2] : nil) {
case ("get-input", _): getDefault(kAudioHardwarePropertyDefaultInputDevice)
case ("get-output", _): getDefault(kAudioHardwarePropertyDefaultOutputDevice)
case ("set-input", let n?): setDefault(kAudioHardwarePropertyDefaultInputDevice, kAudioObjectPropertyScopeInput, n)
case ("set-output", let n?): setDefault(kAudioHardwarePropertyDefaultOutputDevice, kAudioObjectPropertyScopeOutput, n)
default: print("usage: audiodev get-input | get-output | set-input <name> | set-output <name>"); exit(2)
}
