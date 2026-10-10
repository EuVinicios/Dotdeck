"""Run the voice gateway: ``python3 -m voice_gateway`` (settings from the environment)."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from .call import VOICES  # noqa: E402
from .config import Config  # noqa: E402
from .http_api import Gateway  # noqa: E402

FORMAT = "%(levelname)s %(name)s: %(message)s"
# syslog priorities, understood by journald as a "<N>" line prefix
PRIORITIES = {logging.DEBUG: 7, logging.INFO: 6, logging.WARNING: 4, logging.ERROR: 3,
              logging.CRITICAL: 2}


class JournalFormatter(logging.Formatter):
    """Prefix each line with its syslog priority, so journald files warnings as warnings.

    journald makes one entry per line: every line of a traceback gets the prefix too."""

    def format(self, record: logging.LogRecord) -> str:
        prefix = f"<{PRIORITIES.get(record.levelno, 6)}>"
        return "\n".join(prefix + line for line in super().format(record).split("\n"))


def logs_to_journal() -> bool:
    """True when stderr is the journal stream systemd gave us (``$JOURNAL_STREAM``)."""
    try:
        device, inode = (int(n) for n in os.environ.get("JOURNAL_STREAM", "").split(":"))
        stat = os.fstat(sys.stderr.fileno())
    except (ValueError, OSError, AttributeError):
        return False
    return (stat.st_dev, stat.st_ino) == (device, inode)


def setup_logging() -> None:
    """Plain lines on stderr: the systemd unit files them under ``opendots-voice-gateway``
    (``journalctl --user -t opendots-voice-gateway``). ``VOICE_GATEWAY_DEBUG=1`` adds
    per-turn and audio-level details. Nothing logged here ever contains a key or token."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JournalFormatter(FORMAT) if logs_to_journal()
                         else logging.Formatter(FORMAT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    debug = os.environ.get("VOICE_GATEWAY_DEBUG", "").lower() in ("1", "true", "yes", "on")
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    threading.excepthook = log_thread_error


def log_thread_error(args: threading.ExceptHookArgs) -> None:
    """A crash in a call's worker thread is logged as an error, not a bare traceback."""
    if issubclass(args.exc_type, SystemExit):
        return
    name = args.thread.name if args.thread is not None else "?"
    logging.getLogger("voice_gateway").error(
        "Unexpected error in thread %s", name,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def main() -> int:
    setup_logging()
    log = logging.getLogger("voice_gateway")
    try:
        config = Config.from_env()
    except ValueError as error:
        log.error("%s", error)
        return 2
    Gst.init(None)
    missing = [n for n in ("webrtcbin", "nicesrc", "dtlssrtpenc", "sctpenc", "opusenc",
                           "opusdec", "rtpopuspay", "rtpopusdepay", "audiomixer", "appsrc",
                           "appsink") if Gst.ElementFactory.find(n) is None]
    if missing:
        log.error("Missing GStreamer elements: %s", ", ".join(missing))
        return 3
    for name, value in (("VOICE_GATEWAY_VOICE", config.voice),
                        ("VOICE_GATEWAY_FORCE_VOICE", config.voice_override)):
        if value and value not in VOICES:
            log.warning("%s=%s is not one of the voices the audio models know (%s)",
                        name, value[:40], ", ".join(sorted(VOICES)))
    try:
        gateway = Gateway(config)
    except OSError as error:
        # Usually a gateway from an older install still holds the port (server/README.md).
        log.error("Cannot listen on %s:%d: %s. Is another voice gateway still running?",
                  config.host, config.port, error.strerror or error)
        return 4
    loop = GLib.MainLoop()
    threading.Thread(target=loop.run, daemon=True).start()
    log.info("Listening on %s (model %s, voice %s, transcription %s, up to %d calls)",
             gateway.url, config.model, config.voice_override or config.voice,
             config.transcribe_model or "off", config.max_calls)

    def stop(signum, _frame):
        log.info("Stopping (%s)", signal.Signals(signum).name)
        threading.Thread(target=gateway.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    gateway.serve_forever()
    loop.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
