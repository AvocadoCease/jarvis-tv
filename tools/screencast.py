"""Capture the Mac screen with ffmpeg and serve it as a live MPEG-TS stream for a DLNA TV.

Each GET starts its own ffmpeg so the TV always receives the stream from its
first keyframe (old Samsung decoders show grey if they join mid-stream).
"""
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "0.0.0.0", 8766
FPS = int(sys.argv[1]) if len(sys.argv) > 1 else 25
# Constant-bitrate with filler fills the TV's byte-sized buffer faster, cutting delay.
RATE = sys.argv[2] if len(sys.argv) > 2 else ("6M" if FPS > 30 else "4M")

FFMPEG = [
    "ffmpeg", "-hide_banner", "-loglevel", "error",
    "-f", "avfoundation", "-capture_cursor", "1", "-framerate", "60",
    "-pixel_format", "nv12", "-i", "4:none",
    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
    "-vf", f"scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p",
    "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
    "-profile:v", "main", "-level", "3.2" if FPS > 30 else "3.1",
    "-b:v", RATE, "-minrate", RATE, "-maxrate", RATE, "-bufsize", "1M",
    "-g", str(FPS), "-keyint_min", str(FPS), "-sc_threshold", "0", "-x264-params", "repeat-headers=1:nal-hrd=cbr:filler=1",
    "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
    "-f", "mpegts", "-mpegts_flags", "resend_headers", "-",
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def _headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "video/mpeg")
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000")
        self.end_headers()

    def do_HEAD(self):
        self._headers()

    def do_GET(self):
        self._headers()
        proc = subprocess.Popen(FFMPEG, stdout=subprocess.PIPE)
        try:
            while chunk := proc.stdout.read(188 * 64):
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.kill()
            proc.wait()


ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
