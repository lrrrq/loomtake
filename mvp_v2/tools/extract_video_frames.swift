import AVFoundation
import AppKit
import Foundation

let arguments = CommandLine.arguments
guard arguments.count >= 4, (arguments.count - 2) % 2 == 0 else {
    fputs("usage: extract_video_frames.swift <video> <seconds> <output> [...pair]\n", stderr)
    exit(2)
}

let videoURL = URL(fileURLWithPath: arguments[1])
let asset = AVURLAsset(url: videoURL)
let generator = AVAssetImageGenerator(asset: asset)
generator.appliesPreferredTrackTransform = true
generator.requestedTimeToleranceBefore = .zero
generator.requestedTimeToleranceAfter = .zero

var index = 2
while index < arguments.count {
    guard let seconds = Double(arguments[index]) else {
        fputs("invalid seconds: \(arguments[index])\n", stderr)
        exit(2)
    }
    let outputURL = URL(fileURLWithPath: arguments[index + 1])
    do {
        var actualTime = CMTime.zero
        let image = try generator.copyCGImage(
            at: CMTime(seconds: seconds, preferredTimescale: 600),
            actualTime: &actualTime
        )
        let bitmap = NSBitmapImageRep(cgImage: image)
        guard let data = bitmap.representation(using: .png, properties: [:]) else {
            throw NSError(domain: "mvp_v2.video_frames", code: 2)
        }
        try FileManager.default.createDirectory(
            at: outputURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try data.write(to: outputURL, options: .atomic)
    } catch {
        fputs("frame extraction failed at \(seconds)s: \(error)\n", stderr)
        exit(1)
    }
    index += 2
}
