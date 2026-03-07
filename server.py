"""
FETCH — Render-Ready Backend
Gets direct stream URLs from yt-dlp and returns them to the frontend.
The browser streams/downloads directly — no server bandwidth used.
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
import os
import shutil

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
    print("[FETCH] static-ffmpeg loaded")
except Exception as e:
    print(f"[FETCH] static-ffmpeg warning: {e}")

app = Flask(__name__)
CORS(app, origins=["*"])

SECRET_COOKIES = "/etc/secrets/cookies.txt"
COOKIES_FILE = "/tmp/cookies.txt"

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


def get_ydl_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
            }
        },
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


@app.route("/resolve", methods=["POST"])
def resolve():
    """Returns direct stream URLs + video info — no downloading on server."""
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data or not data.get("url"):
        return jsonify({"error": "No URL provided"}), 400

    try:
        opts = get_ydl_opts()
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(data["url"], download=False)

        if not info:
            return jsonify({"error": "Could not extract video info"}), 400

        # Build format list sorted by quality
        formats = []
        for f in info.get("formats", []):
            if not f.get("url"):
                continue
            height = f.get("height")
            fps = f.get("fps")
            ext = f.get("ext", "")
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            filesize = f.get("filesize") or f.get("filesize_approx")

            # Skip storyboards / thumbnails
            if ext in ("mhtml", "vtt") or vcodec == "none" and acodec == "none":
                continue

            label = ""
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
                "http_headers": f.get("http_headers", {}),
            })

        # Sort: combined streams first, then by height desc
        formats.sort(key=lambda x: (
            0 if (x["has_video"] and x["has_audio"]) else 1,
            -(x["height"] or 0)
        ))

        # Pick best combined stream for default player
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
            "stream_headers": best.get("http_headers", {}) if best else {},
            "formats": formats[:20],  # top 20 formats
        })

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
        opts = get_ydl_opts()
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
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
