"""
ASC-Shorts Backend
Downloads a YouTube video and extracts short clips from it.

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --reload --port 8000

Requirements on the host machine:
    - ffmpeg installed and available on PATH
    - yt-dlp installed (pip install yt-dlp)
"""

import os
import uuid
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

APP_DIR = Path(__file__).parent
JOBS_DIR = APP_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="ASC-Shorts Backend")

# Allow requests from your Netlify frontend (and localhost for testing)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://asc-shorts.netlify.app",
        "http://localhost:3000",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve generated clips as static files at /files/<job_id>/<filename>
app.mount("/files", StaticFiles(directory=str(JOBS_DIR)), name="files")


class GenerateRequest(BaseModel):
    url: str
    clip_length: int = 30   # seconds per short
    num_clips: int = 3      # how many shorts to produce


def run(cmd: list[str]):
    """Run a subprocess command and raise with output on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
    return result.stdout


def get_video_duration(path: Path) -> float:
    out = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    return float(out.strip())


@app.post("/generate")
def generate(req: GenerateRequest):
    job_id = uuid.uuid4().hex[:10]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    source_path = job_dir / "source.mp4"

    # 1. Download the source video with yt-dlp
    try:
        run([
            "yt-dlp",
            "-f", "mp4[height<=1080]/best[ext=mp4]",
            "-o", str(source_path),
            req.url,
        ])
    except RuntimeError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"Could not download video: {e}")

    if not source_path.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="Download failed: no file produced.")

    # 2. Figure out duration and split into N evenly spaced clips
    try:
        duration = get_video_duration(source_path)
    except RuntimeError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Could not read video: {e}")

    clip_len = min(req.clip_length, max(5, int(duration)))
    num_clips = max(1, min(req.num_clips, 10))
    spacing = max(0, (duration - clip_len) / max(1, num_clips))

    clip_urls = []
    for i in range(num_clips):
        start = i * spacing
        if start + clip_len > duration:
            start = max(0, duration - clip_len)

        out_name = f"short_{i+1}.mp4"
        out_path = job_dir / out_name

        # 3. Cut the clip and crop to vertical 9:16 (centered)
        try:
            run([
                "ffmpeg", "-y",
                "-ss", str(start),
                "-i", str(source_path),
                "-t", str(clip_len),
                "-vf",
                "crop=ih*9/16:ih,scale=1080:1920",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac",
                str(out_path),
            ])
        except RuntimeError as e:
            continue  # skip a failed clip rather than failing the whole job

        clip_urls.append(f"/files/{job_id}/{out_name}")

    # Remove the large source file, keep only the generated shorts
    source_path.unlink(missing_ok=True)

    if not clip_urls:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Failed to generate any clips.")

    return {"job_id": job_id, "clips": clip_urls}


@app.get("/health")
def health():
    return {"status": "ok"}
