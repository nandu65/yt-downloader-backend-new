"""
FETCH — Render-Ready Backend
Cookies are loaded from Render Secret Files at /etc/secrets/cookies.txt
"""

from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp
import os
import uuid
import threading
import time

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
    print("[FETCH] static-ffmpeg loaded")
except Exception as e:
    print(f"[FETCH] static-ffmpeg warning: {e}")

app = Flask(__name__)
CORS(app, origins=["*"])

TEMP_DIR = "/tmp/fetch_downloads"
COOKIES_FILE = "/etc/secrets/cookies.txt"
os.makedirs(TEMP_DIR, exist_ok=True)

ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "")

if os.path.exists(COOKIES_FILE):
    print("[FETCH] cookies.txt found at /etc/secrets/ ✓")
else:
    print("[FETCH] cookies.txt NOT found at /etc/secrets/")


def check_auth(req):
    if not ACCESS_PASSWORD:
        return True
    return req.headers.get("X-Access-Token", "") == ACCESS_PASSWORD


def cleanup_file(path, delay=60):
    def _delete():
        time.sleep(delay)
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
    threading.Thread(target=_delete, daemon=True).start()


def build_ydl_opts(data, out_path):
    fmt = data.get("format", "bestvideo+bestaudio/best")
    ext = data.get("ext", "mp4")
    opts = {
        "format": fmt,
        "outtmpl": out_path,
        "noplaylist": not data.get("playlist", False),
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [],
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    if ext not in ("mp3", "m4a"):
        opts["merge_output_format"] = ext
    else:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"].append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": ext,
            "preferredquality": "192",
        })
    if data.get("subtitles"):
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = ["en"]
    if data.get("thumbnail"):
        opts["writethumbnail"] = True
        opts["postprocessors"].append({"key": "EmbedThumbnail"})
    if data.get("metadata"):
        opts["postprocessors"].append({"key": "FFmpegMetadata"})
    return opts


@app.route("/download", methods=["POST"])
def download():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    if not data or not data.get("url"):
        return jsonify({"error": "No URL provided"}), 400

    ext = data.get("ext", "mp4")
    job_id = str(uuid.uuid4())
    out_template = os.path.join(TEMP_DIR, f"{job_id}.%(ext)s")

    try:
        opts = build_ydl_opts(data, out_template)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(data["url"], download=True)
            title = info.get("title", "video") if info else "video"

        out_file = next(
            (os.path.join(TEMP_DIR, f) for f in os.listdir(TEMP_DIR) if f.startswith(job_id)),
            None
        )
        if not out_file:
            return jsonify({"error": "File not found after download"}), 500

        actual_ext = out_file.rsplit(".", 1)[-1]
        mime_map = {
            "mp4": "video/mp4", "mkv": "video/x-matroska",
            "webm": "video/webm", "mp3": "audio/mpeg", "m4a": "audio/mp4",
        }
        safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip()[:80]

        def generate():
            with open(out_file, "rb") as f:
                while chunk := f.read(262144):
                    yield chunk
            cleanup_file(out_file, delay=30)

        return Response(
            stream_with_context(generate()),
            mimetype=mime_map.get(actual_ext, "application/octet-stream"),
            headers={
                "Content-Disposition": f'attachment; filename="{safe_title}.{actual_ext}"',
                "Content-Length": str(os.path.getsize(out_file)),
            }
        )
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)}"}), 500


@app.route("/info", methods=["POST"])
def get_info():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    if not data or not data.get("url"):
        return jsonify({"error": "No URL provided"}), 400
    try:
        ydl_opts = {"quiet": True}
        if os.path.exists(COOKIES_FILE):
            ydl_opts["cookiefile"] = COOKIES_FILE
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
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
    return jsonify({"status": "ok", "service": "FETCH", "cookies": os.path.exists(COOKIES_FILE)})


@app.route("/debug", methods=["GET"])
def debug():
    info = {
        "cookies_file_exists": os.path.exists(COOKIES_FILE),
        "cookies_file_size": os.path.getsize(COOKIES_FILE) if os.path.exists(COOKIES_FILE) else 0,
        "cookies_first_3_lines": [],
        "cookies_line_count": 0,
    }
    if os.path.exists(COOKIES_FILE):
        with open(COOKIES_FILE) as f:
            lines = f.readlines()
        info["cookies_line_count"] = len(lines)
        info["cookies_first_3_lines"] = [l.rstrip() for l in lines[:3]]
    return jsonify(info)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
