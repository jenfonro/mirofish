"""Profile-faithful HTTP/1.1 header serialization.

HTTPX serializes requests through h11, and h11 follows the RFC 7230
recommendation of emitting ``Host`` as the first header field regardless of
where the caller placed it.  The official Mirasim clients (Node's ``http``
module in the desktop, reqwest inside the bundled Codex binary) send ``Host``
and ``Connection`` as the *last* two fields, which is exactly how every
capture-derived golden profile records them.  A relay that reorders one field
is trivially distinguishable on the wire by anything that logs raw header
order, so this module teaches h11 to write the fields in the order the request
model already holds them.

Only the writer changes.  Parsing, framing, and header validation stay h11's.
"""

from __future__ import annotations

import logging

import h11
from h11 import _headers, _writers

logger = logging.getLogger(__name__)

_INSTALLED_FLAG = "_mirofish_profile_order"


def _write_headers_in_profile_order(headers, write) -> None:
    """Emit each header in request-model order, casing preserved."""
    for raw_name, _name, value in headers._full_items:
        write(b"%s: %s\r\n" % (raw_name, value))
    write(b"\r\n")


def install_profile_header_order() -> bool:
    """Make h11 serialize headers in the order the request carries them.

    Idempotent.  Returns ``True`` when the profile-order writer is active.  If
    this h11 build no longer exposes the internals the writer relies on, the
    stock writer is left in place and a warning is logged rather than failing
    every upstream request: the relay still works, only the field order
    differs, and ``tests/test_wire_profile.py`` catches that regression.
    """
    if getattr(_writers, _INSTALLED_FLAG, False):
        return True
    if not hasattr(_headers.Headers, "_full_items") \
            or not callable(getattr(_writers, "write_headers", None)):
        logger.warning(
            "h11 %s does not expose the header writer; Host stays first on the wire",
            getattr(h11, "__version__", "?"))
        return False
    _writers.write_headers = _write_headers_in_profile_order
    setattr(_writers, _INSTALLED_FLAG, True)
    return True


def profile_header_order_installed() -> bool:
    return bool(getattr(_writers, _INSTALLED_FLAG, False))
