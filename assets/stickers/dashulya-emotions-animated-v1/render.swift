// macOS Core Graphics animation. Usage: render emotion source.png frames-directory
import Foundation
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers

let args = CommandLine.arguments
guard args.count == 4 else { fatalError("Usage: render emotion source.png frames-directory") }
let emotion = args[1]
guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: args[2]) as CFURL, nil),
      let character = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fatalError("Cannot read character PNG")
}
let output = URL(fileURLWithPath: args[3], isDirectory: true)
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
let duration = 2.8
let pi = Double.pi
func smooth(_ a: Double, _ b: Double, _ x: Double) -> Double {
    let u = min(1, max(0, (x-a)/(b-a)))
    return u*u*(3-2*u)
}
func envelope(_ t: Double) -> Double { smooth(0.15, 0.55, t)*(1-smooth(2.1, 2.65, t)) }
func color(_ r: Double, _ g: Double, _ b: Double, _ a: Double = 1) -> CGColor {
    CGColor(red: r, green: g, blue: b, alpha: a)
}
let white = color(1, 1, 1)
func shape(_ c: CGContext, _ path: CGPath, _ fill: CGColor, _ width: Double = 2.5) {
    c.addPath(path)
    c.setFillColor(fill)
    c.setStrokeColor(white)
    c.setLineWidth(width)
    c.setLineJoin(.round)
    c.drawPath(using: .fillStroke)
}
func sparkle(_ c: CGContext, _ x: Double, _ y: Double, _ radius: Double, _ opacity: Double) {
    let p = CGMutablePath()
    for i in 0..<8 {
        let a = Double(i)*pi/4
        let r = i % 2 == 0 ? radius : radius*0.25
        let point = CGPoint(x: x+sin(a)*r, y: y+cos(a)*r)
        if i == 0 { p.move(to: point) } else { p.addLine(to: point) }
    }
    p.closeSubpath()
    c.saveGState()
    c.setAlpha(opacity)
    shape(c, p, color(1, 0.77, 0.26), 2)
    c.restoreGState()
}

for frame in 0..<84 {
    autoreleasepool {
        let t = Double(frame)/30
        let phase = 2*pi*t/duration
        let e = envelope(t)
        let breath = (1-cos(phase))/2
        var x = 256.0, y = 247.0, sx = 1.0, sy = 1.0, angle = 0.0
        switch emotion {
        case "joy":
            let bounce = pow(sin(phase), 2)*e
            y += 12*bounce
            sx -= 0.014*bounce
            sy += 0.018*bounce
            angle = 2.2*sin(phase)*e
        case "sad":
            y -= 7*breath
            angle = -1.8*breath
            sy -= 0.012*breath
        case "angry":
            let shake = sin(2*pi*4*t)*e*(1-smooth(1.5, 2, t))
            x += 4*shake
            angle = 1.3*shake
            y += 3*breath
        case "surprised":
            let recoil = smooth(0.25, 0.43, t)*(1-smooth(0.65, 1.4, t))
            let spring = sin(2*pi*2.8*max(0, t-0.43))*exp(-3*max(0, t-0.43))*e
            y += 11*recoil + 4*spring
            sx -= 0.035*recoil
            sy += 0.025*recoil + 0.012*spring
            angle = -2*recoil
        case "skeptical":
            let tilt = smooth(0.2, 0.95, t)*(1-smooth(1.8, 2.65, t))
            angle = 4.5*tilt
            x -= 3*tilt
            y += 2*tilt
        default: fatalError("Unknown emotion: \(emotion)")
        }
        let c = CGContext(data: nil, width: 1024, height: 1024, bitsPerComponent: 8,
                          bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                          bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        c.scaleBy(x: 2, y: 2)
        c.interpolationQuality = .high
        c.saveGState()
        c.translateBy(x: x, y: y)
        c.rotate(by: angle*pi/180)
        c.scaleBy(x: sx, y: sy)
        c.draw(character, in: CGRect(x: -218, y: -218, width: 436, height: 436))
        c.restoreGState()

        switch emotion {
        case "joy":
            for i in 0..<5 {
                let positions: [(Double, Double)] = [(77, 360), (420, 393), (133, 438), (445, 290), (366, 452)]
                let u = (t-0.25-Double(i)*0.15)/1.25
                if u > 0 && u < 1 {
                    let p = positions[i]
                    let pulse = sin(pi*u)
                    sparkle(c, p.0, p.1+14*u, (8+Double(i%3)*3)*pulse, pulse)
                }
            }
        case "sad":
            for i in 0..<4 {
                let u = (t-0.3-Double(i)*0.37)/0.95
                if u > 0 && u < 1 {
                    let left = i % 2 == 0
                    let dx = (left ? 246.0 : 333.0)-5*u
                    let dy = (left ? 281.0 : 264.0)-64*u*u-7*breath
                    c.saveGState()
                    c.translateBy(x: dx, y: dy)
                    c.setAlpha(smooth(0, 0.13, u)*(1-smooth(0.65, 1, u)))
                    let p = CGMutablePath()
                    p.move(to: CGPoint(x: 0, y: 10))
                    p.addCurve(to: CGPoint(x: 0, y: -5), control1: CGPoint(x: -10, y: -1), control2: CGPoint(x: -6, y: -5))
                    p.addCurve(to: CGPoint(x: 0, y: 10), control1: CGPoint(x: 8, y: -5), control2: CGPoint(x: 4, y: 2))
                    p.closeSubpath()
                    shape(c, p, color(0.44, 0.83, 1, 0.85), 1.4)
                    c.setFillColor(white)
                    c.fillEllipse(in: CGRect(x: -2, y: 0, width: 2.5, height: 5))
                    c.restoreGState()
                }
            }
        case "angry":
            c.saveGState()
            c.setAlpha(e)
            c.translateBy(x: 418, y: 397+2*sin(phase*3))
            c.scaleBy(x: 1+0.12*sin(phase*3), y: 1+0.12*sin(phase*3))
            let p = CGMutablePath()
            for i in 0..<4 {
                let a = Double(i)*pi/2
                let ux = cos(a), uy = sin(a), vx = -uy, vy = ux
                p.move(to: CGPoint(x: 8*ux+16*vx, y: 8*uy+16*vy))
                p.addQuadCurve(to: CGPoint(x: 16*ux+8*vx, y: 16*uy+8*vy), control: CGPoint(x: 6*ux+6*vx, y: 6*uy+6*vy))
            }
            c.addPath(p); c.setStrokeColor(white); c.setLineWidth(8); c.setLineCap(.round); c.strokePath()
            c.addPath(p); c.setStrokeColor(color(1, 0.18, 0.32)); c.setLineWidth(4.5); c.strokePath()
            c.restoreGState()
        case "surprised":
            let burst = smooth(0.25, 0.45, t)*(1-smooth(1.2, 1.8, t))
            for i in 0..<5 {
                let a = (-25+Double(i)*24)*pi/180
                let length = 12+7*sin(pi*burst)
                let dx = 256+sin(a)*205, dy = 279+cos(a)*190
                let p = CGMutablePath()
                p.move(to: CGPoint(x: dx, y: dy))
                p.addLine(to: CGPoint(x: dx+sin(a)*length, y: dy+cos(a)*length))
                c.saveGState(); c.setAlpha(burst)
                c.setLineCap(.round)
                c.addPath(p); c.setStrokeColor(white); c.setLineWidth(8); c.strokePath()
                c.addPath(p); c.setStrokeColor(color(1, 0.72, 0.24)); c.setLineWidth(4); c.strokePath()
                c.restoreGState()
            }
        case "skeptical":
            for i in 0..<3 {
                let alpha = smooth(0.65+Double(i)*0.23, 0.88+Double(i)*0.23, t)*(1-smooth(2, 2.5, t))
                c.saveGState(); c.setAlpha(alpha)
                let rect = CGRect(x: 399+Double(i)*22, y: 388+3*sin(phase+Double(i)*0.6), width: 10, height: 10)
                c.setFillColor(color(0.48, 0.31, 0.64)); c.setStrokeColor(white); c.setLineWidth(2.5)
                c.addEllipse(in: rect); c.drawPath(using: .fillStroke)
                c.restoreGState()
            }
        default: break
        }
        let image = c.makeImage()!
        let url = output.appendingPathComponent(String(format: "%03d.png", frame))
        let destination = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil)!
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else { fatalError("PNG export failed") }
    }
}
print("Rendered \(emotion): 84 transparent frames.")
