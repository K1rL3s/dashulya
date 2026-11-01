# Dashulya: animated emotions v1

Six animated emotion stickers: five new animations and the previously finished
air kiss. The artwork is preserved; each emotion uses a distinct loop of
character movement and animated effects.

| File | Emoji | Animation |
| --- | --- | --- |
| [01-joy.webm](webm/01-joy.webm) | 😄 | Bouncy laugh with gold sparkles |
| [02-kiss.webm](webm/02-kiss.webm) | 😘 | Existing air kiss, preserved unchanged |
| [03-sad.webm](webm/03-sad.webm) | 😢 | Slow downward movement with falling tears |
| [04-angry.webm](webm/04-angry.webm) | 😠 | Short head shakes with a pulsing comic anger mark |
| [05-surprised.webm](webm/05-surprised.webm) | 😮 | Springy recoil with surprise rays |
| [06-skeptical.webm](webm/06-skeptical.webm) | 🤨 | Slow questioning tilt with three appearing dots |

All six files are transparent 512 × 512 VP9 WebM video stickers, 2.8 seconds
at 30 FPS, without audio. Each stays below Telegram's 256 KB limit. See
[Telegram's requirements](https://core.telegram.org/stickers#video-stickers-and-emoji).

## Preview and download

- [preview.html](preview.html): interactive gallery, light/dark background and pause control.
- [preview.gif](preview.gif): all six animations together, on a dark background.
- [preview.png](preview.png): still contact sheet in the same order as the table.
- [dashulya-emotions-animated-v1.zip](dashulya-emotions-animated-v1.zip): six uploadable WebM files and short upload instructions.
- [validation.json](validation.json): per-file dimensions, size, duration, frame count, decoded alpha and loop-boundary checks.

Open [@Stickers](https://t.me/Stickers), create a video sticker set and upload
the WebM files as files/documents. Assign the corresponding emoji. Files are
prepared locally; publishing a pack is a separate action.

## Sources and rebuild

The five new character animations reuse images directly from
[the original PNG set](../dashulya-emotions-v1/README.md). Original image-generation
prompts are recorded in [the static pack](../dashulya-emotions-v1/PROMPTS.md).
The existing `webm/02-kiss.webm` is retained as the finished kiss asset.
No new images or model calls were needed for this batch. Faces and expressions
belong to those drawings; the animation adds movement and independently drawn effects.

[render.swift](render.swift) renders all motion and effects locally with macOS
Core Graphics. [build.sh](build.sh) compiles it and encodes the five new files with
FFmpeg. Run from the project root on macOS with Swift and FFmpeg installed:

```sh
sh assets/stickers/dashulya-emotions-animated-v1/build.sh
```

The existing `webm/02-kiss.webm` remains in place during a rebuild.

Intermediate frames and the compiled renderer are kept under ignored
`.scratch/dashulya-emotions-animated-v1/`. Encoder versions can affect size;
recheck the 256 KB limit after rebuilding. Force FFmpeg's `libvpx-vp9` decoder
when inspecting the separate alpha plane.
