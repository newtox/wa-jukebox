#!/usr/bin/env python3
"""
WA Jukebox: a Spotify Connect speaker (librespot) whose sound is streamed as MP3 over HTTP,
so that a jukebox in WorkAdventure can play it ("play audio" property with the stream URL).

    librespot (raw PCM) -> pacer (real time, silence while nothing plays) -> ffmpeg (MP3) -> HTTP listeners

The stream never stops: when Spotify is paused or no one is connected, listeners get silence,
so the jukebox keeps working as soon as you press play again.
"""
import fcntl
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = os.environ.get("DATA_DIR", "/data")
NAME = os.environ.get("DEVICE_NAME", "WorkAdventure Jukebox")
STREAM_PATH = "/" + os.environ.get("STREAM_PATH", "stream").strip("/")
PORT = int(os.environ.get("PORT", "8000"))
MP3_BITRATE = os.environ.get("MP3_BITRATE", "192k")
SPOTIFY_BITRATE = os.environ.get("SPOTIFY_BITRATE", "320")
INITIAL_VOLUME = os.environ.get("INITIAL_VOLUME", "80")
# How the Spotify volume slider maps to loudness: log (sounds even), cubic, linear. librespot has no way to ignore
# the slider, so it always sets the volume for everyone (with the same few seconds of delay as everything else).
VOLUME_CTRL = os.environ.get("VOLUME_CTRL", "log")
# For tests: any command that writes 44.1 kHz stereo s16le PCM to stdout.
LIBRESPOT_CMD = os.environ.get("LIBRESPOT_CMD")
CREDENTIALS = os.path.join(DATA, "credentials.json")
F_SETPIPE_SZ = 1031

RATE, CHANNELS, SAMPLE_BYTES = 44100, 2, 2
BYTES_PER_SEC = RATE * CHANNELS * SAMPLE_BYTES
TICK = 0.02                                        # 20 ms of audio per step
CHUNK = int(BYTES_PER_SEC * TICK) // 4 * 4
BUFFER_MAX = int(BYTES_PER_SEC * 0.3) // 4 * 4     # at most 300 ms ahead, so pause/skip react quickly
PREROLL = int(BYTES_PER_SEC * 0.1) // 4 * 4        # wait for 100 ms before (re)starting, avoids stutter
SILENCE = bytes(CHUNK)


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, file=sys.stderr, flush=True)


# ---------- PCM from librespot ----------

pcm = bytearray()
pcm_lock = threading.Condition()


def librespot_cmd():
    if LIBRESPOT_CMD:
        return shlex.split(LIBRESPOT_CMD)
    return [
        "librespot", "--name", NAME, "--backend", "pipe", "--bitrate", SPOTIFY_BITRATE,
        "--cache", DATA, "--system-cache", DATA, "--disable-audio-cache", "--disable-discovery",
        "--initial-volume", INITIAL_VOLUME, "--volume-ctrl", VOLUME_CTRL, "--enable-volume-normalisation",
    ]


def login_running() -> bool:
    """Whether `jukebox-login` is still open. Both would register the same device, and closing the
    login afterwards would sign the jukebox out of Spotify Connect again."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if b"--enable-oauth" in f.read():
                    return True
        except OSError:
            pass
    return False


def run_librespot():
    """Keeps librespot running (once logged in) and moves its PCM into the buffer."""
    hinted = False
    while True:
        if not LIBRESPOT_CMD and not os.path.exists(CREDENTIALS):
            if not hinted:
                log("Not logged in to Spotify yet. Run:  docker exec -it <container> jukebox-login")
                hinted = True
            time.sleep(3)
            continue
        if not LIBRESPOT_CMD and login_running():
            log("waiting for jukebox-login to be closed (Ctrl+C)")
            while login_running():
                time.sleep(2)
            time.sleep(3)
        log("starting librespot as", repr(NAME))
        proc = subprocess.Popen(librespot_cmd(), stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
        try:                                       # small pipe: librespot may only be ~25 ms ahead of us
            fcntl.fcntl(proc.stdout.fileno(), F_SETPIPE_SZ, 4096)
        except OSError:
            pass
        while True:
            data = proc.stdout.read1(16384)
            if not data:
                break
            with pcm_lock:
                while len(pcm) >= BUFFER_MAX:      # back pressure: librespot only runs in real time
                    pcm_lock.wait()
                pcm.extend(data)
        code = proc.wait()
        log(f"librespot stopped (exit {code}), restarting in 5 s")
        time.sleep(5)


# ---------- real-time pacing + MP3 encoding ----------

def encoder():
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(RATE), "-ac", str(CHANNELS),
         "-i", "pipe:0", "-c:a", "libmp3lame", "-b:a", MP3_BITRATE, "-write_xing", "0", "-flush_packets", "1",
         "-f", "mp3", "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )


def pace(ff):
    """Feeds ffmpeg exactly in real time: music from the buffer, or silence when there is none."""
    playing = False
    next_tick = time.monotonic()
    while True:
        with pcm_lock:
            if not playing and len(pcm) >= PREROLL:
                playing = True
            if playing and len(pcm) >= CHUNK:
                out = bytes(pcm[:CHUNK])
                del pcm[:CHUNK]
            elif playing and pcm:
                out = bytes(pcm) + bytes(CHUNK - len(pcm))
                pcm.clear()
                playing = False
            else:
                out = SILENCE
                playing = False
            pcm_lock.notify_all()
        ff.stdin.write(out)
        ff.stdin.flush()
        next_tick += TICK
        delay = next_tick - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        elif delay < -1:                           # fell far behind (e.g. machine was suspended): resync
            next_tick = time.monotonic()


# ---------- HTTP listeners ----------

listeners: set = set()
listeners_lock = threading.Lock()


def broadcast(ff):
    while True:
        data = ff.stdout.read1(4096)
        if not data:
            log("encoder stopped")
            os._exit(1)
        with listeners_lock:
            for q in list(listeners):
                try:
                    q.put_nowait(data)
                except queue.Full:                 # listener too slow: drop it, the player reconnects
                    listeners.discard(q)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.end_headers()

    def do_HEAD(self):
        if self.path.split("?")[0] != STREAM_PATH:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            body = f"ok, {len(listeners)} listener(s), logged in: {os.path.exists(CREDENTIALS) or bool(LIBRESPOT_CMD)}\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)
            return
        if path != STREAM_PATH:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("icy-name", NAME)
        self._cors()
        self.end_headers()
        q: queue.Queue = queue.Queue(maxsize=256)
        with listeners_lock:
            listeners.add(q)
        log(f"listener joined ({len(listeners)} now)")
        try:
            while True:
                try:
                    data = q.get(timeout=10)
                except queue.Empty:
                    break
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with listeners_lock:
                listeners.discard(q)
            log(f"listener left ({len(listeners)} now)")


def main():
    os.makedirs(DATA, exist_ok=True)
    ff = encoder()
    threading.Thread(target=run_librespot, daemon=True).start()
    threading.Thread(target=pace, args=(ff,), daemon=True).start()
    threading.Thread(target=broadcast, args=(ff,), daemon=True).start()
    log(f"streaming on :{PORT}{STREAM_PATH}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
