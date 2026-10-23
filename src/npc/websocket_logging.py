"""Logging shared by the mobile and overlay WebSocket servers."""

import logging


class _EmptyHandshakeFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if (record.levelno != logging.ERROR or record.msg != "opening handshake failed"
                or not record.exc_info):
            return True

        from websockets.exceptions import InvalidMessage

        error = record.exc_info[1]
        if not isinstance(error, InvalidMessage):
            return True
        request_error = error.__cause__
        if (not isinstance(request_error, EOFError)
                or str(request_error) != "connection closed while reading HTTP request line"):
            return True
        stream_error = request_error.__cause__
        if (not isinstance(stream_error, EOFError)
                or str(stream_error) != "stream ends after 0 bytes, before end of line"):
            return True

        # TCP probes / browser preconnects can close without sending a request.
        # Match the full chain: partial requests and malformed HTTP remain errors.
        record.levelno = logging.DEBUG
        record.levelname = logging.getLevelName(logging.DEBUG)
        return logger.isEnabledFor(logging.DEBUG)


# Scope the filter to our servers; don't change the library's global logger.
logger = logging.getLogger(__name__)
logger.addFilter(_EmptyHandshakeFilter())
