"""Free Reel renderer: stock clips + Edge TTS voiceover + animated captions -> 1080x1920 MP4.
Runs inside GitHub Actions. Input comes from the n8n workflow via env vars JOB_ID and PAYLOAD.
"""
import asyncio
import json
import os
import subprocess
import urllib.request

import edge_tts

W, H, FPS = 1080, 1920, 30
WORDS_PER_LINE = 3
FONT = "DejaVu Sans"

job_id = os.environ["JOB_ID"]
job = json.loads(os.environ["PAYLOAD"])
voice = job.get("voice") or "en-IN-PrabhatNeural"
headline = job.get("headline", "")
scenes = job["scenes"]

os.makedirs("work", exist_ok=True)
os.makedirs("out", exist_ok=True)


def run(cmd):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, check=True).stdout
    return float(out.strip())


async def tts(text, path):
    words = []
    try:
        com = edge_tts.Communicate(text, voice, boundary="WordBoundary")
    except TypeError:  # older edge-tts versions always emit word boundaries
        com = edge_tts.Communicate(text, voice)
    with open(path, "wb") as f:
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                words.append([start, start + chunk["duration"] / 1e7, chunk["text"]])
    return words


def even_words(text, total):
    # Fallback when the voice gives no word timings: spread words by length.
    parts = text.split()
    weights = [len(p) + 1 for p in parts]
    step = total / max(sum(weights), 1)
    t, out = 0.0, []
    for p, w in zip(parts, weights):
        out.append([t, t + w * step, p])
        t += w * step
    return out


def ass_time(t):
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def clean(text):
    return text.replace("{", "(").replace("}", ")").replace("\n", " ")


all_words = []
offset = 0.0
video_list, audio_list = [], []

for i, sc in enumerate(scenes):
    clip = f"work/clip_{i}.mp4"
    req = urllib.request.Request(sc["clip"], headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(clip, "wb") as f:
        f.write(r.read())

    mp3 = f"work/voice_{i}.mp3"
    words = asyncio.run(tts(sc["text"], mp3))
    wav = f"work/voice_{i}.wav"
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp3, "-ar", "48000", "-ac", "2", wav])
    dur = duration(wav)
    if not words:
        words = even_words(sc["text"], dur)

    seg = f"work/seg_{i}.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-i", clip, "-t", f"{dur:.3f}",
         "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1",
         "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", seg])

    for w in words:
        all_words.append([w[0] + offset, w[1] + offset, w[2]])
    offset += dur
    video_list.append(seg)
    audio_list.append(wav)

with open("work/videos.txt", "w") as f:
    f.writelines(f"file '{os.path.basename(p)}'\n" for p in video_list)
with open("work/audios.txt", "w") as f:
    f.writelines(f"file '{os.path.basename(p)}'\n" for p in audio_list)
run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "work/videos.txt", "-c", "copy", "work/video.mp4"])
run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "work/audios.txt", "-c", "copy", "work/voice.wav"])

# Captions: 3 words at a time, yellow, bold, caps. Headline at the top for the first 4 seconds.
lines = [
    "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "",
    "[V4+ Styles]",
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
    "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    f"Style: Caption,{FONT},96,&H0000D4FF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,2,60,60,520,1",
    f"Style: Headline,{FONT},72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,3,18,0,8,80,80,260,1",
    "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
]
if headline:
    lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(min(4.0, offset))},Headline,,0,0,0,,{clean(headline)}")
groups = [all_words[i:i + WORDS_PER_LINE] for i in range(0, len(all_words), WORDS_PER_LINE)]
for gi, g in enumerate(groups):
    start = g[0][0]
    end = groups[gi + 1][0][0] if gi + 1 < len(groups) else max(g[-1][1], offset)
    text = clean(" ".join(w[2] for w in g)).upper()
    lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,{text}")
with open("work/subs.ass", "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")

out = f"out/{job_id}.mp4"
run(["ffmpeg", "-y", "-loglevel", "error", "-i", "work/video.mp4", "-i", "work/voice.wav",
     "-vf", "ass=work/subs.ass", "-c:v", "libx264", "-preset", "medium", "-crf", "21",
     "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(FPS),
     "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-shortest", "-movflags", "+faststart", out])
print(f"Rendered {out} ({offset:.1f}s)")
