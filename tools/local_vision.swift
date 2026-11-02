// Local Vision bridge for the Layer Rebuild alpha.
//
// It deliberately uses only macOS's built-in Vision framework: no image is
// uploaded and no model credit is spent.  It reports recognised text regions
// and writes one foreground-instance mask per salient object.
//
// Usage:
//   swift tools/local_vision.swift <image-path> <output-directory>

import AppKit
import CoreImage
import Foundation
import Vision

struct TextRegion: Codable {
    let name: String
    let text: String
    let confidence: Float
    let left: Int
    let top: Int
    let width: Int
    let height: Int
}

struct InstanceRegion: Codable {
    let name: String
    let mask: String
}

struct VisionReport: Codable {
    let width: Int
    let height: Int
    let text: [TextRegion]
    let instances: [InstanceRegion]
    let labels: [String]
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

guard CommandLine.arguments.count == 3 else {
    fail("Usage: local_vision.swift <image-path> <output-directory>")
}

let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
let outputURL = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
try FileManager.default.createDirectory(at: outputURL, withIntermediateDirectories: true)

guard let image = NSImage(contentsOf: imageURL) else {
    fail("Could not load image.")
}
var rect = NSRect(origin: .zero, size: image.size)
guard let cgImage = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) else {
    fail("Could not decode image pixels.")
}

let width = cgImage.width
let height = cgImage.height
let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])

let textRequest = VNRecognizeTextRequest()
textRequest.recognitionLevel = .accurate
textRequest.usesLanguageCorrection = true
textRequest.recognitionLanguages = ["zh-Hans", "en-US"]
let classifyRequest = VNClassifyImageRequest()

do {
    try handler.perform([textRequest, classifyRequest])
} catch {
    fail("Vision analysis failed: \(error.localizedDescription)")
}

var textRegions: [TextRegion] = []
for (index, observation) in (textRequest.results ?? []).enumerated() {
    guard let candidate = observation.topCandidates(1).first else { continue }
    let box = observation.boundingBox
    let left = max(0, Int((box.minX * CGFloat(width)).rounded(.down)))
    let top = max(0, Int(((1 - box.maxY) * CGFloat(height)).rounded(.down)))
    let regionWidth = min(width - left, max(1, Int((box.width * CGFloat(width)).rounded(.up))))
    let regionHeight = min(height - top, max(1, Int((box.height * CGFloat(height)).rounded(.up))))
    textRegions.append(TextRegion(
        name: String(format: "Text %02d", index + 1),
        text: candidate.string,
        confidence: candidate.confidence,
        left: left,
        top: top,
        width: regionWidth,
        height: regionHeight
    ))
}

var instances: [InstanceRegion] = []
let context = CIContext()
let modernHandler = Vision.ImageRequestHandler(cgImage)
let foregroundRequest = Vision.GenerateForegroundInstanceMaskRequest()
if let observation = try await modernHandler.perform(foregroundRequest) {
    for (offset, instance) in observation.allInstances.enumerated() {
        do {
            let selected = IndexSet(integer: instance)
            let mask = try observation.generateScaledMask(for: selected, scaledToImageFrom: modernHandler)
            let ciImage = CIImage(cvPixelBuffer: mask)
            guard let maskCGImage = context.createCGImage(ciImage, from: ciImage.extent) else { continue }
            let data = NSBitmapImageRep(cgImage: maskCGImage).representation(using: .png, properties: [:])
            let fileName = String(format: "object-%02d-mask.png", offset + 1)
            let maskURL = outputURL.appendingPathComponent(fileName)
            try data?.write(to: maskURL)
            instances.append(InstanceRegion(name: String(format: "Object %02d", offset + 1), mask: fileName))
        } catch {
            continue
        }
    }
}

let labels = (classifyRequest.results ?? [])
    .filter { $0.confidence >= 0.03 }
    .prefix(12)
    .map { $0.identifier }
let report = VisionReport(width: width, height: height, text: textRegions, instances: instances, labels: labels)
let json = try JSONEncoder().encode(report)
FileHandle.standardOutput.write(json)
