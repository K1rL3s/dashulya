#!/bin/sh
set -eu

# Run from the repository root. Requires macOS, Swift, and FFmpeg.
pack=assets/stickers/dashulya-emotions-animated-v1
work=.scratch/dashulya-emotions-animated-v1
mkdir -p "$work" "$pack/webm" .scratch/swift-module-cache
swiftc -module-cache-path .scratch/swift-module-cache "$pack/render.swift" -o "$work/render"

for name in 01-joy 03-sad 04-angry 05-surprised 06-skeptical; do
    emotion=${name#*-}
    source=assets/stickers/dashulya-emotions-v1/png/$name.png
    "$work/render" "$emotion" "$source" "$work/$name"
    ffmpeg -hide_banner -loglevel error -y -framerate 30 \
        -i "$work/$name/%03d.png" \
        -vf 'scale=480:480:flags=lanczos,format=rgba,pad=512:512:16:16:color=black@0,format=yuva420p' -an \
        -c:v libvpx-vp9 -pix_fmt yuva420p -b:v 400k -crf 36 \
        -deadline good -cpu-used 3 -row-mt 1 -auto-alt-ref 0 \
        -metadata title="Dashulya — $emotion" "$pack/webm/$name.webm"
done
# The existing webm/02-kiss.webm is the preserved, finished kiss sticker.
test -f "$pack/webm/02-kiss.webm"
