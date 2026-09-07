from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
VIDEO_DIR = ROOT / "docs" / "video"
FRAME_DIR = VIDEO_DIR / "frames"
AUDIO_DIR = VIDEO_DIR / "audio"
SCENE_DIR = VIDEO_DIR / "scenes"
OUTPUT = VIDEO_DIR / "OpenRouter-CrowdBench-overview.mp4"
RAW_OUTPUT = VIDEO_DIR / "OpenRouter-CrowdBench-overview-raw.mp4"
TARGET_SECONDS = 119.5

VOICE_DIRECTION = (
    "Upbeat, lifelike female product narrator. Sound warm, intelligent, optimistic, and conversational, "
    "with natural breath pauses and varied intonation. Avoid an announcer voice. Use energetic emphasis on "
    "the problem and confident reassurance when explaining privacy. Pronounce OpenRouter as Open Router and "
    "CrowdBench as Crowd Bench. Speak clearly at approximately 150 words per minute."
)

SCENES = [
    {
        "frame": "01-hero.jpg",
        "title": "Too many models. Not enough trustworthy evidence.",
        "direction": "Open with friendly urgency, then lift the energy on free and paid models.",
        "text": (
            "Building an AI project at home should be exciting. But choosing among OpenRouter's free and paid models "
            "can feel like guesswork. Which route is actually worth using today?"
        ),
    },
    {
        "frame": "01-hero.jpg",
        "title": "A model listing is not a reliability guarantee",
        "direction": "Sound practical and empathetic. Stress the day-to-day variability.",
        "text": (
            "Catalog listings do not tell the whole story. Routes change. A model may answer quickly today, then rate-limit, "
            "return an empty response, or ignore formatting tomorrow."
        ),
    },
    {
        "frame": "02-security-controls.jpg",
        "title": "Bring your key. Keep control of it.",
        "direction": "Slow slightly and sound reassuring and transparent.",
        "text": (
            "OpenRouter CrowdBench turns isolated experiences into comparable evidence. Bring your own key to contribute; "
            "the app keeps it in memory only, never in public history or browser storage."
        ),
    },
    {
        "frame": "03-history-entry.jpg",
        "title": "One fixed probe. Comparable observations.",
        "direction": "Confidently emphasize fixed probe and shared picture.",
        "text": (
            "Every smoke test uses the same fixed, versioned probe. Observations from different people and days become "
            "comparable, building a shared picture that improves with every contribution."
        ),
    },
    {
        "frame": "11-saved-run.jpg",
        "title": "Smoke-test broadly first",
        "direction": "Pick up the pace and make the workflow feel easy.",
        "text": (
            "Start broad with a one-request smoke test. Choose free models, the complete catalog, historically successful "
            "routes, or your own list. Safety limits keep the request budget explicit."
        ),
    },
    {
        "frame": "12-run-results.jpg",
        "title": "Then measure repeat reliability",
        "direction": "Give repeat reliability and evidence a crisp verbal beat.",
        "text": (
            "Move responding models into repeated reliability testing. CrowdBench tracks probe success, instruction compliance, "
            "median latency, rate limits, and an overall routing-quality score."
        ),
    },
    {
        "frame": "13-evidence-modal.jpg",
        "title": "Inspect the evidence behind every verdict",
        "direction": "Sound analytical but approachable.",
        "text": (
            "Open the stable evidence panel to see what happened probe by probe. Every healthy, fragile, or unavailable "
            "verdict is supported by status, timing, and output evidence."
        ),
    },
    {
        "frame": "03-history-entry.jpg",
        "title": "Current price, refreshed automatically",
        "direction": "Brighten on current pricing and cost awareness.",
        "text": (
            "Current OpenRouter input and output prices refresh when the site loads and when a test starts. Filter free "
            "versus paid routes, and compare reliability directly with cost."
        ),
    },
    {
        "frame": "04-trends.jpg",
        "title": "See routing quality change over time",
        "direction": "Sound curious and insight-oriented.",
        "text": (
            "The historical dashboard reveals the pattern: models observed, total probes, community success, rate-limit "
            "responses, and route trends. The strongest route is not always the most famous."
        ),
    },
    {
        "frame": "05-latency-trend.jpg",
        "title": "Toggle reliability, latency, scores, and 429s",
        "direction": "Use distinct beats for each metric, with a small lift at the end.",
        "text": (
            "Toggle routing success, overall score, median latency, and four-twenty-nine rate. See whether a route is "
            "improving, degrading, or simply too inconsistent for unattended use."
        ),
    },
    {
        "frame": "07-price-sort.jpg",
        "title": "Sort the evidence your way",
        "direction": "Sound decisive and useful.",
        "text": (
            "Sort by current price, success, instruction pass rate, latency, rate limits, or date. Quickly find a practical "
            "balance between capability, availability, speed, and budget."
        ),
    },
    {
        "frame": "09-community-sort.jpg",
        "title": "More contributors create stronger evidence",
        "direction": "Warm, community-minded, and inclusive.",
        "text": (
            "Anonymous unique-tester counts show how broadly results are supported without using API keys as identity. "
            "More contributors, accounts, and locations make the evidence more representative and resilient."
        ),
    },
    {
        "frame": "10-latency-rate-limits.jpg",
        "title": "Test a model. Help everyone route better.",
        "direction": "Build to an upbeat, memorable call to action. Pause briefly before the final sentence.",
        "text": (
            "If you use OpenRouter for a home project, visit OpenRouter CrowdBench on GitHub. Explore the history, run a "
            "small test, and contribute what you learn. Test a model—and help everyone route better."
        ),
    },
]


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def media_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def load_openai_key() -> str:
    if os.getenv("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]
    env_file = ROOT.parents[1] / ".env"
    if env_file.exists():
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError("OPENAI_API_KEY is not configured")


def synthesize(index: int, scene: dict[str, str]) -> Path:
    output = AUDIO_DIR / f"{index:02d}.wav"
    api_key = load_openai_key()
    request = {
        "model": "gpt-4o-mini-tts",
        "voice": "marin",
        "input": scene["text"],
        "instructions": f"{VOICE_DIRECTION} Scene direction: {scene['direction']}",
        "response_format": "wav",
        "speed": 1.25,
    }
    for attempt in range(5):
        request_object = Request(
            "https://api.openai.com/v1/audio/speech",
            data=json.dumps(request).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request_object, timeout=180) as response:
                output.write_bytes(response.read())
                return output
        except HTTPError as exc:
            if exc.code != 429 or attempt == 4:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Speech generation failed ({exc.code}): {detail}") from exc
            time.sleep(min(30, 4 * (2**attempt)))
    raise RuntimeError("Speech generation did not complete")


def render_scene(index: int, scene: dict[str, str], audio: Path) -> Path:
    output = SCENE_DIR / f"{index:02d}.mp4"
    seconds = media_duration(audio) + 0.30
    frames = max(1, round(seconds * 30))
    fade_out = max(0.0, seconds - 0.28)
    title_file = SCENE_DIR / f"{index:02d}-title.txt"
    title_file.write_text(scene["title"], encoding="utf-8")
    horizontal = "iw/2-(iw/zoom/2)" if index % 2 else "iw/2-(iw/zoom/2)+(iw-iw/zoom)*0.10"
    vertical = "ih/2-(ih/zoom/2)" if index % 3 else "ih/2-(ih/zoom/2)+(ih-ih/zoom)*0.08"
    video_filter = (
        "scale=1344:756:force_original_aspect_ratio=increase,crop=1280:720,"
        f"zoompan=z='min(zoom+0.00013,1.045)':x='{horizontal}':y='{vertical}':d={frames}:s=1280x720:fps=30,"
        "drawbox=x=0:y=620:w=1280:h=100:color=0x10261c@0.90:t=fill:enable='between(t,0.8,5.8)',"
        f"drawtext=fontfile='/System/Library/Fonts/Supplemental/Arial Bold.ttf':textfile='{title_file}':"
        "fontcolor=white:fontsize=29:x=46:y=650:enable='between(t,1.0,5.6)',"
        "drawbox=x=46:y=692:w=190:h=4:color=0x55d49a@1:t=fill:enable='between(t,1.2,5.6)',"
        f"fade=t=in:st=0:d=0.25,fade=t=out:st={fade_out:.3f}:d=0.28,format=yuv420p"
    )
    run(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-i", str(FRAME_DIR / scene["frame"]),
        "-i", str(audio), "-vf", video_filter,
        "-af", "loudnorm=I=-16:LRA=7:TP=-1.5,aresample=48000,apad",
        "-t", f"{seconds:.3f}", "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "22",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(output),
    )
    return output


def main() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("ffmpeg and ffprobe are required")
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    SCENE_DIR.mkdir(parents=True, exist_ok=True)
    videos: list[Path] = []
    for index, scene in enumerate(SCENES, 1):
        audio = synthesize(index, scene)
        videos.append(render_scene(index, scene, audio))
    concat_file = SCENE_DIR / "concat.txt"
    concat_file.write_text("".join(f"file '{video.resolve()}'\n" for video in videos), encoding="utf-8")
    run(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-c", "copy", "-movflags", "+faststart", str(RAW_OUTPUT),
    )
    raw_seconds = media_duration(RAW_OUTPUT)
    tempo = raw_seconds / TARGET_SECONDS
    if 0.98 <= tempo <= 1.02:
        shutil.move(RAW_OUTPUT, OUTPUT)
    else:
        run(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(RAW_OUTPUT),
            "-filter_complex", f"[0:v]setpts=PTS/{tempo:.8f}[v];[0:a]atempo={tempo:.8f}[a]",
            "-map", "[v]", "-map", "[a]", "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "22",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(OUTPUT),
        )
        RAW_OUTPUT.unlink(missing_ok=True)
    print(f"Created {OUTPUT} ({media_duration(OUTPUT):.1f} seconds)")


if __name__ == "__main__":
    main()
