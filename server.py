"""
FETCH — Render-Ready Backend
- /resolve  → returns stream URLs (used by player, works for YouTube too)
- /download → server-side download + stream to browser (for Instagram/TikTok/Reddit/etc)
"""

from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp
import os
import uuid
import shutil
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
SECRET_COOKIES = "/etc/secrets/cookies.txt"
COOKIES_FILE = "/tmp/cookies.txt"
os.makedirs(TEMP_DIR, exist_ok=True)

ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "")

if os.path.exists(SECRET_COOKIES):
    shutil.copy2(SECRET_COOKIES, COOKIES_FILE)
    print("[FETCH] cookies.txt copied to /tmp ✓")
else:
    print("[FETCH] No cookies.txt found")


def check_auth(req):
    if not ACCESS_PASSWORD:
        return True
    return req.headers.get("X-Access-Token", "") == ACCESS_PASSWORD


def base_ydl_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web", "tv_embedded"],
            }
        },
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def cleanup_file(path, delay=60):
    def _delete():
        time.sleep(delay)
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
    threading.Thread(target=_delete, daemon=True).start()


# ── /resolve — returns direct stream URLs (no server download) ───────────────
@app.route("/resolve", methods=["POST"])
def resolve():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    if not data or not data.get("url"):
        return jsonify({"error": "No URL provided"}), 400

    try:
        opts = base_ydl_opts()
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(data["url"], download=False)

        if not info:
            return jsonify({"error": "Could not extract video info"}), 400

        formats = []
        for f in info.get("formats", []):
            if not f.get("url"):
                continue
            ext = f.get("ext", "")
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            height = f.get("height")
            fps = f.get("fps")
            filesize = f.get("filesize") or f.get("filesize_approx")

            if ext in ("mhtml", "vtt") or (vcodec == "none" and acodec == "none"):
                continue

            if vcodec != "none" and height:
                label = f"{height}p"
                if fps and fps > 30:
                    label += f" {int(fps)}fps"
                if acodec == "none":
                    label += " (video only)"
            elif acodec != "none" and vcodec == "none":
                abr = f.get("abr")
                label = f"Audio {int(abr)}kbps" if abr else "Audio only"
            else:
                label = f"{height}p" if height else ext.upper()

            formats.append({
                "format_id": f.get("format_id"),
                "label": label,
                "ext": ext,
                "url": f.get("url"),
                "height": height or 0,
                "has_video": vcodec != "none",
                "has_audio": acodec != "none",
                "filesize": filesize,
            })

        formats.sort(key=lambda x: (
            0 if (x["has_video"] and x["has_audio"]) else 1,
            -(x["height"] or 0)
        ))

        best = next((f for f in formats if f["has_video"] and f["has_audio"]), None)
        if not best and formats:
            best = formats[0]

        return jsonify({
            "title": info.get("title"),
            "duration": info.get("duration"),
            "uploader": info.get("uploader"),
            "thumbnail": info.get("thumbnail"),
            "platform": info.get("extractor_key"),
            "stream_url": best["url"] if best else None,
            "stream_ext": best["ext"] if best else "mp4",
            "formats": formats[:20],
        })

    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)}"}), 500


# ── /download — server downloads and streams file to browser ─────────────────
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

    opts = base_ydl_opts()
    opts["outtmpl"] = out_template
    opts["noplaylist"] = not data.get("playlist", False)
    opts["format"] = "bestvideo+bestaudio/bestvideo*+bestaudio/best"
    opts["postprocessors"] = []

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
    if data.get("metadata"):
        opts["postprocessors"].append({"key": "FFmpegMetadata"})

    try:
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
        opts = base_ydl_opts()
        with yt_dlp.YoutubeDL(opts) as ydl:
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
    return jsonify({
        "status": "ok",
        "service": "FETCH",
        "cookies": os.path.exists(COOKIES_FILE)
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
