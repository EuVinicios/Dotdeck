"""Run the voice gateway: ``python3 -m voice_gateway`` (settings from the environment)."""

from __future__ import annotations

import logging
import signal
import sys
import threading

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from .config import Config  # noqa: E402
from .http_api import Gateway  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        config = Config.from_env()
    except ValueError as error:
        print(f"voice-gateway: {error}", file=sys.stderr)
        return 2
    Gst.init(None)
    missing = [n for n in ("webrtcbin", "nicesrc", "dtlssrtpenc", "sctpenc", "opusenc",
                           "opusdec", "rtpopuspay", "rtpopusdepay", "audiomixer", "appsrc",
                           "appsink") if Gst.ElementFactory.find(n) is None]
    if missing:
        print(f"voice-gateway: missing GStreamer elements: {', '.join(missing)}", file=sys.stderr)
        return 3
    loop = GLib.MainLoop()
    threading.Thread(target=loop.run, daemon=True).start()
    gateway = Gateway(config)
    logging.getLogger("voice_gateway").info(
        "Listening on %s (model %s, voice %s)", gateway.url, config.model, config.voice)

    def stop(*_args):
        threading.Thread(target=gateway.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    gateway.serve_forever()
    loop.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
