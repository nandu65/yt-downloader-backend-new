"""
FETCH — Render-Ready Backend
Streams downloaded video directly to the user's browser, then deletes the temp file.
No persistent storage needed — works on Render free tier.

Deploy on Render as a Web Service:
  - Build Command:  pip install -r requirements.txt
  - Start Command:  gunicorn server:app
  - Environment:    Python 3
"""

from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp
import os
import uuid
import threading
import time

app = Flask(__name__)

# Allow requests from your frontend (update with your actual Netlify/Vercel URL)
CORS(app, origins=[
    "http://localhost",
    "http://127.0.0.1",
    "http://localhost:3000",
    "https://*.netlify.app",
    "https://*.vercel.app",
    # Add your custom domain here, e.g.: "https://fetch.yourdomain.com"
])

# Temp dir for downloads — Render has an ephemeral /tmp
TEMP_DIR = "/tmp/fetch_downloads"
os.makedirs(TEMP_DIR, exist_ok=True)

# Optional: password protect your instance
# Set ACCESS_PASSWORD env var on Render to enable
ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "")


def check_auth(req):
    if not ACCESS_PASSWORD:
        return True
    token = req.headers.get("X-Access-Token", "")
    return token == ACCESS_PASSWORD


def cleanup_file(path: str, delay: int = 60):
    """Delete file after a delay (gives browser time to finish downloading)."""
    def _delete():
        time.sleep(delay)
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
    threading.Thread(target=_delete, daemon=True).start()


def build_ydl_opts(data: dict, out_path: str) -> dict:
    fmt = data.get("format", "bestvideo+bestaudio/best")
    ext = data.get("ext", "mp4")
    subtitles = data.get("subtitles", False)
    thumbnail = data.get("thumbnail", False)
    playlist = data.get("playlist", False)
    metadata = data.get("metadata", False)

    opts = {
        "format": fmt,
        "outtmpl": out_path,
        "noplaylist": not playlist,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [],
    }

    # Merge video+audio into mp4/mkv
    if ext not in ("mp3", "m4a"):
        opts["merge_output_format"] = ext
    else:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"].append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": ext,
            "preferredquality": "192",
        })

    if subtitles:
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = ["en"]

    if thumbnail:
        opts["writethumbnail"] = True
        opts["postprocessors"].append({"key": "EmbedThumbnail"})

    if metadata:
        opts["postprocessors"].append({"key": "FFmpegMetadata"})

    return opts


@app.route("/download", methods=["POST"])
def download():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data or not data.get("url"):
        return jsonify({"error": "No URL provided"}), 400

    url = data["url"]
    ext = data.get("ext", "mp4")

    # Unique temp filename
    job_id = str(uuid.uuid4())
    out_template = os.path.join(TEMP_DIR, f"{job_id}.%(ext)s")

    try:
        opts = build_ydl_opts(data, out_template)

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "video") if info else "video"

        # Find the actual output file (yt-dlp resolves the extension)
        out_file = None
        for f in os.listdir(TEMP_DIR):
            if f.startswith(job_id):
                out_file = os.path.join(TEMP_DIR, f)
                break

        if not out_file or not os.path.exists(out_file):
            return jsonify({"error": "File not found after download"}), 500

        actual_ext = out_file.rsplit(".", 1)[-1]

        # MIME types
        mime_map = {
            "mp4": "video/mp4", "mkv": "video/x-matroska",
            "webm": "video/webm", "mp3": "audio/mpeg",
            "m4a": "audio/mp4", "mov": "video/quicktime",
        }
        mime = mime_map.get(actual_ext, "application/octet-stream")

        # Safe filename for download
        safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip()[:80]
        download_name = f"{safe_title}.{actual_ext}"

        # Stream file to browser
        def generate():
            with open(out_file, "rb") as f:
                while chunk := f.read(1024 * 256):  # 256KB chunks
                    yield chunk
            # Schedule cleanup after streaming
            cleanup_file(out_file, delay=30)

        response = Response(
            stream_with_context(generate()),
            mimetype=mime,
            headers={
                "Content-Disposition": f'attachment; filename="{download_name}"',
                "Content-Length": str(os.path.getsize(out_file)),
                "X-Video-Title": safe_title,
            }
        )
        return response

    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)}"}), 500


@app.route("/info", methods=["POST"])
def get_info():
    """Fetch video metadata without downloading."""
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data or not data.get("url"):
        return jsonify({"error": "No URL provided"}), 400

    try:
        with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
            info = ydl.extract_info(data["url"], download=False)
            return jsonify({
                "title": info.get("title"),
                "duration": info.get("duration"),
                "uploader": info.get("uploader"),
                "thumbnail": info.get("thumbnail"),
                "platform": info.get("extractor_key"),
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "FETCH"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
