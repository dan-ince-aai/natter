// Natter.app: a menu bar app that runs the Python engine (app.py) as its child, so macOS asks for
// microphone and system-audio permission for "Natter" rather than for Terminal or your editor.
// The menu switches Listen / Talk on and off and shows a click-through subtitle overlay above every app.
// The main window is native (WKWebView in an NSWindow with a transparent, full-size title bar), not a browser.
import AppKit
import WebKit

let base = "http://127.0.0.1:18765"

struct LaneState { var running = false; var target = ""; var source = "" }

/// A transparent strip over the page's header that drags the window, like a title bar.
final class DragStrip: NSView {
    override var mouseDownCanMoveWindow: Bool { true }
    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 { window?.performZoom(nil) } else { window?.performDrag(with: event) }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate, NSWindowDelegate, WKUIDelegate {
    var window: NSWindow?
    var web: WKWebView?
    var item: NSStatusItem!
    var proc: Process?
    var dir = ""
    var dataDir = ""
    var hearItem: NSMenuItem!
    var speakItem: NSMenuItem!
    var overlayItem: NSMenuItem!
    var overlay: NSPanel?
    var lanes: [String: LaneState] = [:]

    func applicationDidFinishLaunching(_ note: Notification) {
        let info = Bundle.main.infoDictionary ?? [:]
        // Two ways to run: from a source checkout (LIProjectDir/LIPython point at it), or packaged (LIBundled: the
        // engine and a private Python live inside the app; settings, key and logs go to Application Support).
        let bundled = info["LIBundled"] as? Bool ?? false
        let res = Bundle.main.resourcePath ?? ""
        dir = bundled ? res + "/app" : (info["LIProjectDir"] as? String ?? "")
        let python = bundled ? res + "/python/bin/python3" : (info["LIPython"] as? String ?? "/usr/bin/python3")
        dataDir = bundled ? NSHomeDirectory() + "/Library/Application Support/Natter" : dir

        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        setIcon(active: false)
        let menu = NSMenu()
        menu.delegate = self
        hearItem = NSMenuItem(title: "Listen", action: #selector(toggleHear), keyEquivalent: "h")
        speakItem = NSMenuItem(title: "Talk", action: #selector(toggleSpeak), keyEquivalent: "s")
        overlayItem = NSMenuItem(title: "Subtitles overlay", action: #selector(toggleOverlay), keyEquivalent: "t")
        menu.addItem(hearItem)
        menu.addItem(speakItem)
        menu.addItem(overlayItem)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Open Natter", action: #selector(openUI), keyEquivalent: "o"))
        menu.addItem(NSMenuItem(title: "Restore sound", action: #selector(restoreSound), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Show log", action: #selector(showLog), keyEquivalent: "l"))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit Natter", action: #selector(quit), keyEquivalent: "q"))
        menu.items.forEach { $0.target = self }
        item.menu = menu

        let logs = dataDir + "/logs"
        try? FileManager.default.createDirectory(atPath: logs, withIntermediateDirectories: true)  // also creates dataDir
        FileManager.default.createFile(atPath: logs + "/app.log", contents: nil)
        let log = FileHandle(forWritingAtPath: logs + "/app.log")

        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = ["-u", dir + "/app.py", "--no-window"]
        p.currentDirectoryURL = URL(fileURLWithPath: dir)
        var env = ProcessInfo.processInfo.environment
        env["NATTER_DATA"] = dataDir
        env["PYTHONDONTWRITEBYTECODE"] = "1"  // never write into the signed bundle
        p.environment = env
        p.standardOutput = log
        p.standardError = log
        p.terminationHandler = { _ in DispatchQueue.main.async { NSApp.terminate(nil) } }
        do { try p.run(); proc = p } catch {
            let a = NSAlert()
            a.messageText = "Natter couldn't start"
            a.informativeText = "\(python) \(dir)/app.py: \(error.localizedDescription)"
            a.runModal()
            NSApp.terminate(nil)
        }
        Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in self?.refresh() }
        setupMainMenu()
        waitForEngine(tries: 60)
    }

    /// Open the window once the engine answers.
    func waitForEngine(tries: Int) {
        guard let url = URL(string: base + "/api/lanes") else { return }
        URLSession.shared.dataTask(with: url) { data, _, _ in
            DispatchQueue.main.async {
                if data != nil { self.openUI() }
                else if tries > 0 { DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.waitForEngine(tries: tries - 1) } }
            }
        }.resume()
    }

    /// App and Edit menus, so copy and paste work in the window while it's in front.
    func setupMainMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Reload", action: #selector(reloadUI), keyEquivalent: "r").target = self
        appMenu.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        appMenu.addItem(withTitle: "Quit Natter", action: #selector(quit), keyEquivalent: "q").target = self
        appItem.submenu = appMenu
        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        NSApp.mainMenu = main
    }

    /// The pixel critter: a template silhouette when idle (matches light and dark menu bars), full colour while
    /// translating. Falls back to SF Symbols if the images are missing.
    func setIcon(active: Bool) {
        let file = active ? "menubar-live" : "menubar"
        if let img = Bundle.main.image(forResource: file) {
            img.size = NSSize(width: 18, height: 18)
            img.isTemplate = !active
            item.button?.image = img
        } else if let img = NSImage(systemSymbolName: active ? "waveform.circle.fill" : "globe", accessibilityDescription: "Natter") {
            img.isTemplate = true
            item.button?.image = img
        } else {
            item.button?.title = active ? "N•" : "N"
        }
    }

    // MARK: engine state
    func refresh() {
        guard let url = URL(string: base + "/api/lanes") else { return }
        URLSession.shared.dataTask(with: url) { data, _, _ in
            guard let data = data, let json = try? JSONSerialization.jsonObject(with: data) as? [String: [String: Any]] else { return }
            var next: [String: LaneState] = [:]
            for (k, v) in json {
                next[k] = LaneState(running: v["running"] as? Bool ?? false, target: v["target"] as? String ?? "",
                                    source: v["source"] as? String ?? "")
            }
            DispatchQueue.main.async { self.lanes = next; self.updateMenu() }
        }.resume()
    }

    func updateMenu() {
        let hear = lanes["in"] ?? LaneState(), speak = lanes["out"] ?? LaneState()
        hearItem.title = hear.target.isEmpty ? "Listen" : "Listen in \(hear.target)"
        hearItem.state = hear.running ? .on : .off
        speakItem.title = speak.target.isEmpty ? "Talk" : "Talk in \(speak.target)"
        speakItem.state = speak.running ? .on : .off
        overlayItem.state = (overlay?.isVisible ?? false) ? .on : .off
        setIcon(active: hear.running || speak.running)
    }

    func menuWillOpen(_ menu: NSMenu) { updateMenu(); refresh() }

    func post(_ path: String) {
        guard let url = URL(string: base + path) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        URLSession.shared.dataTask(with: req) { data, _, _ in
            if let data = data, let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               (j["ok"] as? Bool) == false, let err = j["error"] as? String {
                DispatchQueue.main.async {
                    let a = NSAlert(); a.messageText = "Natter"; a.informativeText = err; a.runModal()
                }
            }
            self.refresh()
        }.resume()
    }

    @objc func toggleHear() { post((lanes["in"]?.running ?? false) ? "/api/stop/in" : "/api/start/in") }
    @objc func toggleSpeak() { post((lanes["out"]?.running ?? false) ? "/api/stop/out" : "/api/start/out") }

    // MARK: subtitle overlay: transparent, click-through, above every app and full-screen space
    @objc func toggleOverlay() {
        if let o = overlay, o.isVisible { o.orderOut(nil); updateMenu(); return }
        if overlay == nil {
            let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
            let w = screen.width * 0.86, h: CGFloat = 230
            let frame = NSRect(x: screen.midX - w / 2, y: screen.minY + 30, width: w, height: h)
            let panel = NSPanel(contentRect: frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            panel.isOpaque = false
            panel.backgroundColor = .clear
            panel.hasShadow = false
            panel.level = .statusBar
            panel.ignoresMouseEvents = true
            panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
            let web = WKWebView(frame: NSRect(origin: .zero, size: frame.size))
            web.setValue(false, forKey: "drawsBackground")
            web.autoresizingMask = [.width, .height]
            web.load(URLRequest(url: URL(string: base + "/captions?lane=all")!))
            panel.contentView = web
            overlay = panel
        }
        overlay?.orderFrontRegardless()
        updateMenu()
    }

    @objc func openUI() {
        if window == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 600, height: 860),
                             styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                             backing: .buffered, defer: false)
            w.titlebarAppearsTransparent = true
            w.titleVisibility = .hidden
            w.title = "Natter"
            w.backgroundColor = NSColor(red: 0x17 / 255.0, green: 0x11 / 255.0, blue: 0x2b / 255.0, alpha: 1)
            w.minSize = NSSize(width: 480, height: 720)
            w.isReleasedWhenClosed = false
            w.delegate = self
            w.setFrameAutosaveName("NatterMain")
            let content = NSView(frame: w.contentRect(forFrameRect: w.frame))
            let web = WKWebView(frame: content.bounds, configuration: WKWebViewConfiguration())
            web.autoresizingMask = [.width, .height]
            web.setValue(false, forKey: "drawsBackground")
            web.uiDelegate = self
            web.load(URLRequest(url: URL(string: base + "/?native=1")!))
            content.addSubview(web)
            self.web = web
            // Drag strip across the header, leaving the traffic lights (left) and the settings button (right) clickable.
            let h: CGFloat = 50
            let strip = DragStrip(frame: NSRect(x: 80, y: content.bounds.height - h, width: content.bounds.width - 80 - 64, height: h))
            strip.autoresizingMask = [.width, .minYMargin]
            content.addSubview(strip)
            w.contentView = content
            if !w.setFrameUsingName("NatterMain") { w.center() }
            window = w
        }
        NSApp.setActivationPolicy(.regular)  // a Dock icon and ⌘-Tab while the window is open
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func reloadUI() { web?.reload() }

    func windowWillClose(_ notification: Notification) {
        if (notification.object as? NSWindow) == window { NSApp.setActivationPolicy(.accessory) }  // back to menu bar only
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openUI(); return true
    }

    /// Links that open a new window (subtitles) go to the default browser.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    /// Escape hatch: stop translating and put the Mac's output and mic back on real devices.
    @objc func restoreSound() { post("/api/restore-sound") }

    @objc func showLog() { NSWorkspace.shared.open(URL(fileURLWithPath: dataDir + "/logs/app.log")) }

    @objc func quit() {
        proc?.terminationHandler = nil
        proc?.terminate()
        NSApp.terminate(nil)
    }

    func applicationWillTerminate(_ note: Notification) {
        // Ask the engine to stop, and force it after 3 s so a stuck audio teardown can't hang the app.
        if let p = proc, p.isRunning {
            p.terminate()
            let deadline = Date().addingTimeInterval(3)
            while p.isRunning && Date() < deadline { usleep(50_000) }
            if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        }
    }
}

signal(SIGTERM, SIG_IGN)
let termSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
termSource.setEventHandler { NSApp.terminate(nil) }  // so a kill also stops the engine
termSource.resume()

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
