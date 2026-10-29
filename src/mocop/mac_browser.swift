import AppKit
func code(_ s: String) -> FourCharCode { s.utf8.reduce(0) { ($0 << 8) | UInt32($1) } }
func object(_ kind: String, _ identifier: String, _ container: NSAppleEventDescriptor) -> NSAppleEventDescriptor {
    let r = NSAppleEventDescriptor.record()
    r.setDescriptor(NSAppleEventDescriptor(typeCode: code(kind)), forKeyword: code("want"))
    r.setDescriptor(NSAppleEventDescriptor(enumCode: code("ID  ")), forKeyword: code("form"))
    r.setDescriptor(NSAppleEventDescriptor(string: identifier), forKeyword: code("seld"))
    r.setDescriptor(container, forKeyword: code("from"))
    return r.coerce(toDescriptorType: code("obj "))!
}
let args = CommandLine.arguments
let pid = Int32(args[1])!
let window = object("cwin", args[2], NSAppleEventDescriptor.null())
let tab = object("CrTb", args[3], window)
let js = String(data: FileHandle.standardInput.readDataToEndOfFile(), encoding: .utf8)!
let event = NSAppleEventDescriptor.appleEvent(withEventClass: code("CrSu"), eventID: code("ExJa"), targetDescriptor: NSAppleEventDescriptor(processIdentifier: pid), returnID: -1, transactionID: 0)
event.setParam(tab, forKeyword: code("----"))
event.setParam(NSAppleEventDescriptor(string: js), forKeyword: code("JvSc"))
do {
 let reply = try event.sendEvent(options: [.waitForReply], timeout: 15)
 if let error = reply.paramDescriptor(forKeyword: code("errn")), error.int32Value != 0 {
  fputs("Browser read failed: \(error.int32Value) \(reply.paramDescriptor(forKeyword: code("errs"))?.stringValue ?? "")\n", stderr)
  exit(1)
 }
 print(reply.paramDescriptor(forKeyword: code("----"))?.stringValue ?? "")
} catch { fputs("Browser read failed\n", stderr); exit(1) }
