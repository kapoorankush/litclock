"""GET / — PWA shell front door (Status tab).

M1 rendered an empty shell. M2 (PR #245) filled the Status tab via
`status.html.j2` — the literary hero card + 5-row status list + stale-
quote banner. M3 owns /settings + /api/settings; M4 owns /system; M5
(this milestone) owns /updates + /api/update/* + /api/wifi/reset on
their own blueprints (src/control_server/routes/{updates,wifi}.py).
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, render_template

from .status import collect_status

bp = Blueprint("index", __name__)


@bp.route("/")
def index() -> str:
    # Status tab is the front door — server-renders the literary hero +
    # 5 status rows + (conditional) stale banner so first paint is
    # populated even without JS (PRD §7.5 progressive enhancement).
    status_file_cfg = current_app.config.get("STATUS_FILE")
    update_status_cfg = current_app.config.get("UPDATE_STATUS_FILE")
    last_update_cfg = current_app.config.get("LAST_UPDATE_FILE")
    lkg_sha_cfg = current_app.config.get("LKG_SHA_FILE")
    phase3_skipped_cfg = current_app.config.get("PHASE3_SKIPPED_FILE")
    payload = collect_status(
        status_file=Path(status_file_cfg) if status_file_cfg else None,
        version_override=current_app.config.get("VERSION_OVERRIDE"),
        env_file=current_app.config.get("ENV_FILE"),
        update_status_file=Path(update_status_cfg) if update_status_cfg else None,
        last_update_file=Path(last_update_cfg) if last_update_cfg else None,
        lkg_sha_file=Path(lkg_sha_cfg) if lkg_sha_cfg else None,
        phase3_skipped_file=Path(phase3_skipped_cfg) if phase3_skipped_cfg else None,
    )
    return render_template("status.html.j2", active_tab="status", s=payload)
