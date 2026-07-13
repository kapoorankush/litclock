"""Single source of truth for the Control PWA's listen port and URL.

Both the control_server (which binds the port) and the e-ink clock renderer
(which paints the scan-to-open QR) need the same port and the same URL shape.
Before #343 this was duplicated: the port default lived in ``app.py`` AND
``handoff.py``, and the URL was hardcoded ``http://<host>:8443`` in three
places (``literary_clock.py`` twice, ``handoff.py`` once). This module
collapses all of that into one constant + one builder.

#343: the control_server moved to **port 80** so a recipient never has to type
a port — the QR / mDNS bookmark / typed URL is bare ``http://litclock.local``
or ``http://<ip>``. The port is bound by ``pi`` (a non-root service account)
via the ``net.ipv4.ip_unprivileged_port_start=80`` sysctl drop-in, NOT a
capability — so it never touches the unit's ``NoNewPrivileges=no`` + setuid
``sudo`` wiring (see systemd/litclock-control.service).

``control_base_url`` OMITS the port when it is the HTTP default (80): a URL
with a visible ``:80`` would defeat the whole point of the change. Any other
port (a dev override, or the historical 8443) is rendered explicitly so the
URL stays correct.
"""

import os
from typing import Final

# The one port knob. Default 80 (#343). Overridable via env for dev / tests /
# a non-standard deployment; control_server binds it and the clock's QR
# encodes it, so an override stays consistent across both surfaces.
CONTROL_PORT: Final[int] = int(os.environ.get("LITCLOCK_CONTROL_PORT", "80"))


def control_base_url(host: str) -> str:
    """Return the Control PWA base URL for ``host`` (an IP or ``litclock.local``).

    Omits the port entirely when it is the HTTP default (80) so the URL a user
    sees, scans, or pins has no port to type; renders ``:<port>`` explicitly
    for any other value.

    In practice ``host`` is always an IPv4 literal or ``litclock.local`` (the
    resolvers are ``AF_INET``-only and callers guard empty ⇒ this is the QR
    fallback), but as the now-central URL builder it brackets a bare IPv6
    literal so ``[::1]:8080`` stays parseable rather than colliding with the
    port separator.
    """
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if CONTROL_PORT == 80:
        return f"http://{host}"
    return f"http://{host}:{CONTROL_PORT}"
