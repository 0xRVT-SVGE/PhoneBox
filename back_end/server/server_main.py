# ============================================================
# FILE: back_end/server/server_main.py
# ============================================================
"""
Application entry point.

Startup sequence
────────────────
1.  Scan worker (face + barcode)
2.  WebRTC async loop
3.  Scanner loop (front camera)
4.  Top camera started EAGERLY (feeds top_rolling_buffer from boot)
5.  DVW system
6.  ROI CALIBRATION — two frozen-frame editors (bottom cam, top cam).
    Blocks until the operator confirms both windows.
    Writes rois_bottom.json and rois_top.json in tools/.
7.  Slot monitor (reads rois_bottom.json via generate_grid_rois)
8.  Admin handlers
9.  Flask-SocketIO (blocking)

Shutdown model:
    _stop_event.set() → webrtc_handler.shutdown() → top_camera.stop()
    → op_ctx cleanup → slot_monitor.join() → os._exit(0)
"""

import argparse
import logging
import os
import signal
import threading

from Backup.back_end.scanner_loop import scanner_loop
from Backup.back_end.scanner_state import scanner_state
from Backup.back_end.scanner_worker import scan_worker
from Backup.back_end.server import webrtc_handler
from Backup.back_end.server.app import (
    create_app, get_slot_monitor, get_slot_operations, set_monitor_components
)
from Backup.back_end.server.webrtc_handler import webrtc_bp
from Backup.back_end.server.metrics import register_metrics_endpoint
# from back_end.camera_manager import cam_mgr
from Backup.back_end.slot_monitor.admin.admin_ops_handler import register_admin_handlers
from Backup.back_end.slot_monitor.camera.top_camera import top_camera
from Backup.back_end.slot_monitor.ops_handler import register_dvw_handlers
from Backup.back_end.slot_monitor.services.operation_context import op_ctx

logger = logging.getLogger(__name__)

from Backup.back_end.config import ServerConfig as _SVC
import Backup.back_end.slot_monitor.db_interface as _db_iface

DEBUG_ROI    = _SVC.DEBUG_ROI
DEBUG_WINDOW = _SVC.DEBUG_WINDOW
DEV_MODE = os.getenv(_SVC.DEV_MODE_ENV_VAR, "0") == "1"

_stop_event = threading.Event()

# Runs interactive UI once if camera_config.json is missing,
# then resolves all roles to current indices.
#cam_mgr._dev_mode = DEV_MODE
#cam_mgr._roles = cam_mgr._roles  # unchanged
#cam_mgr.setup_if_needed()

app, socketio = create_app(stop_event=_stop_event)
app.register_blueprint(webrtc_bp, url_prefix="/webrtc")
register_metrics_endpoint(app, get_slot_monitor)
scanner_state.set_socketio(socketio)

#logger.info(f"Camera indices: {cam_mgr.all_indices()}")
#if DEV_MODE:
#    logger.warning("DEV MODE active — same camera may serve multiple roles")


# ============================================================
# SHUTDOWN
# ============================================================

def _shutdown(sig, frame):
    logger.info("=" * 60)
    logger.info(f"Signal {sig} received — shutting down")
    logger.info("=" * 60)

    _stop_event.set()
    webrtc_handler.shutdown()
    top_camera.stop()          # also stops top_rolling_buffer
    op_ctx.stop_cleanup_thread()
    op_ctx.clear_all()

    slot_monitor = get_slot_monitor()
    if slot_monitor:
        slot_monitor.join(timeout=8.0)

    logger.info("All modules stopped — exiting")
    os._exit(0)


# ============================================================
# BOX IDENTITY — resolved once at startup
# ============================================================

def _resolve_box_id(slug: str) -> int:
    """
    Look up the box_id for the given slug in the boxes table.
    Raises RuntimeError if the slug is not found — we want a
    loud early failure rather than silently operating on the
    wrong box's data.
    """
    from Backup.back_end.Database.db import get_conn, put_conn
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT box_id FROM boxes WHERE box_slug = %s;",
                (slug,),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError(
                    f"Unknown BOX_SLUG={slug!r}. "
                    "Run migrations/0001_multi_box.sql and INSERT the box "
                    "into the boxes table before starting the server."
                )
            return int(row[0])
    finally:
        put_conn(conn)


# ============================================================
# WEBSOCKET EVENTS — LEGACY SCANNER
# ============================================================

@socketio.on("toggle_scan")
def handle_toggle_scan(_):
    from Backup.back_end.scanner_worker import start_scan, stop_scan
    client_id = request.sid
    new_state = not scanner_state.scan_request["running"]
    scanner_state.scan_request["running"] = new_state

    if new_state:
        scanner_state.update_last_barcode()
        scanner_state.auth_status.update({"authorized": False, "user": None})
        scanner_state.scan_results.update({
            "face_verified":         False,
            "barcode_verified":      False,
            "current_name":          "Idle",
            "badge_timeout_exceeded": False,
        })
        start_scan(client_id)
    else:
        logger.info(f"Stopping scan for client {client_id}")
        stop_scan()

    scanner_state.emit_to_requester()


@socketio.on("get_status")
def handle_get_status(_):
    scanner_state.emit_to_requester()

from flask import request
"""
@socketio.on("list_cameras")
def on_list_cameras(_):
    client_id = request.sid
    socketio.emit(
        "camera_list",
        cam_mgr.status_dict(),
        to=client_id,
        namespace="/"
    )


@socketio.on("set_camera")
def on_set_camera(data):
    client_id = request.sid

    role  = data.get("role")
    index = data.get("index")

    if role is None or index is None:
        socketio.emit(
            "camera_set_result",
            {"status": "error", "message": "missing role or index"},
            to=client_id,
            namespace="/"
        )
        return

    try:
        cam_mgr.update_role(role, index)

        # Hot-switch top cam
        if role == "top_cam":
            from back_end.slot_monitor.camera import top_camera as tc_mod
            tc_mod.CAMERA_INDEX = index

        socketio.emit(
            "camera_set_result",
            {"status": "success", "role": role, "index": index},
            to=client_id,
            namespace="/"
        )

    except KeyError as e:
        socketio.emit(
            "camera_set_result",
            {"status": "error", "message": str(e)},
            to=client_id,
            namespace="/"
        )

"""
# ============================================================
# MAIN
# ============================================================

# A5: --skip-calibration lets the server restart without blocking on the
# interactive ROI calibration windows. ROI files must already exist from
# a previous calibration run. Use --recalibrate to force re-calibration.
_parser = argparse.ArgumentParser(prog="server_main", add_help=False)
_parser.add_argument(
    "--skip-calibration", action="store_true",
    help="Skip interactive ROI calibration; use existing rois_*.json files.",
)
_args, _ = _parser.parse_known_args()
_SKIP_CALIBRATION = _args.skip_calibration


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("PHONE BOX SERVER — STARTING")
    logger.info("=" * 60)

    # 0. Resolve box identity — fail fast if BOX_SLUG not in DB.
    _box_id = _resolve_box_id(_SVC.BOX_SLUG)
    _db_iface.set_box_id(_box_id)          # thread the box_id into all DB queries
    logger.info(f"Box identity: slug={_SVC.BOX_SLUG!r}, box_id={_box_id}")

    # 1. Scan worker
    threading.Thread(target=scan_worker, daemon=True, name="ScanWorker").start()
    logger.info("Scan worker started")

    # Opt #38: start evidence storage pruner (background retention checks)
    try:
        from Backup.back_end.server.evidence_storage import evidence_store
        evidence_store.start()
        logger.info("Evidence storage pruner started (Opt #38)")
    except Exception as e:
        logger.warning(f"Evidence storage pruner failed (non-fatal): {e}")


    # Opt #16: pre-warm DeepFace — loads SFace model in background so first scan
    # has no cold-start delay (~1-3 s on first DeepFace.represent() call).
    def _prewarm_deepface():
        """Pre-warm face embedding backends (ONNX and DeepFace fallback)."""
        import numpy as np
        # Opt #3: try ONNX first — initialises session and runs one dummy pass
        try:
             from Backup.back_end.face_embedder import prewarm as _onnx_prewarm
             if _onnx_prewarm():
                 logger.info("Face embedder: ONNX Runtime pre-warmed")
                 return   # ONNX is ready; no need to also warm TF
        except Exception as e:
                 logger.debug(f"ONNX prewarm skipped: {e}")
                 # Opt #16: fall back to warming DeepFace/TensorFlow
        try:
             from deepface import DeepFace
             try:
                 import tensorflow as _tf
                 _tf.get_logger().setLevel("ERROR")
             except Exception:
                 pass
             dummy = np.zeros((160, 160, 3), dtype=np.uint8)
             DeepFace.represent(img_path=dummy, model_name="SFace",
                                detector_backend="opencv", enforce_detection=False)
             logger.info("Face embedder: DeepFace (TF) pre-warmed")
        except Exception as e:
             logger.warning(f"DeepFace pre-warm failed (non-fatal): {e}")


    threading.Thread(target=_prewarm_deepface, daemon=True, name="DeepFacePrewarm").start()
    # Opt #17: pre-warm BackgroundEncoder — starts its daemon worker thread
    # immediately so the first alarm clip has no queue-startup latency.
    try:
        from Backup.back_end.slot_monitor.admin.background_encoder import BackgroundEncoder
        BackgroundEncoder.instance()
        logger.info("BackgroundEncoder pre-warmed")
    except Exception as e:
        logger.warning(f"BackgroundEncoder pre-warm failed (non-fatal): {e}")


    # 2. WebRTC async loop
    webrtc_handler.start()

    # 3. Scanner loop (front camera)
    threading.Thread(
        target=lambda: scanner_loop(
            stop_event=_stop_event,
            debugwindow=DEBUG_WINDOW,
            debugroi=DEBUG_ROI,
        ),
        daemon=True,
        name="ScannerLoop",
    ).start()
    logger.info("Scanner loop started")

    # 4. Top camera — eager start so top_rolling_buffer fills from boot.
    #    top_camera.start() also starts top_rolling_buffer (idempotent).
    top_camera.start()
    logger.info("Top camera started (eager)")

    # 5. DVW system
    slot_ops = get_slot_operations()
    if not slot_ops:
        logger.error("Slot operations not available — DVW system DISABLED")
    else:
        register_dvw_handlers(socketio, slot_ops)
        op_ctx.start_cleanup_thread()
        logger.info("DVW system ready")

    # 6. ROI CALIBRATION
    #    Fetch num_lids synchronously using a one-shot DB connection so we
    #    don't depend on the async slot monitor being up yet.
    #    Falls back to 4 if the DB is unreachable (calibration still runs).
    num_lids = _SVC.FALLBACK_NUM_LIDS
    box_lids: list[int] = []          # actual lid values for this box
    try:
        from Backup.back_end.Database.db import get_conn, put_conn
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                # Multi-box aware: fetch the real lid values for this box.
                # Falls back to global MAX(lid)+1 if boxes table not yet migrated.
                cur.execute("""
                    SELECT lid FROM locations
                    WHERE box_id = %s
                    ORDER BY lid;
                """, (_box_id,))
                rows = cur.fetchall()
                if rows:
                    box_lids = [r[0] for r in rows]
                    num_lids = len(box_lids)
                else:
                    # Fallback: no locations row for this box yet (pre-migration
                    # or new box not yet seeded). Use configured fallback rather
                    # than MAX(lid)+1 which would count slots from OTHER boxes.
                    num_lids = _SVC.FALLBACK_NUM_LIDS
                    logger.warning(
                        f"No locations found for box_id={_box_id}. "
                        f"Using FALLBACK_NUM_LIDS={num_lids} for calibration. "
                        "Run DBQuery.sql and seed the locations table for this box."
                    )
        finally:
            put_conn(conn)
        logger.info(
            f"ROI calibration: {num_lids} lids for box_id={_box_id} "
            f"(lids {box_lids[0]}–{box_lids[-1] if box_lids else '?'})"
        )
    except Exception as e:
        logger.warning(
            f"Could not fetch lids from DB ({e}). "
            f"Defaulting to {num_lids} for calibration."
        )

    if _SKIP_CALIBRATION:
        logger.info(
            f"A5: --skip-calibration set — skipping interactive ROI calibration. "
            f"Using existing rois_bottom_{_SVC.BOX_SLUG}.json / rois_top_{_SVC.BOX_SLUG}.json."
        )
    else:
        from Backup.back_end.slot_monitor.tools.roi_calibration import run_calibration
        run_calibration(num_lids, box_slug=_SVC.BOX_SLUG)   # blocks until operator confirms both windows

    # 7. Slot monitor (reads rois_bottom.json written by step 6)
    slot_monitor = get_slot_monitor()
    if slot_monitor:
        slot_monitor.start()
        logger.info("Slot monitor started")
        set_monitor_components(slot_monitor)

        logger.info("Waiting for slot monitor setup to complete...")
        if not slot_monitor._setup_complete.wait(timeout=_SVC.MONITOR_SETUP_TIMEOUT):
            logger.error(
                "Slot monitor setup timed out after 20s — "
                "admin resolution system will not be available"
            )
        elif slot_ops and slot_monitor.alarm:
            # 8. Admin handlers
            register_admin_handlers(socketio, slot_ops, slot_monitor.alarm)
            logger.info("Admin resolution system ready")

            # Opt #39: wire Redis publisher into alarm controller if prepared in app.py
            publisher = getattr(slot_monitor, "_pending_redis_publisher", None)
            if publisher is not None:
                slot_monitor.alarm.set_redis_publisher(publisher)
                del slot_monitor._pending_redis_publisher
                logger.info("Alarm controller: Redis fanout enabled (Opt #39)")

        else:
            logger.warning("Admin resolution system not started — alarm unavailable")
    else:
        logger.warning("Slot monitor not available")

    # 9. Signal handlers
    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    logger.info("Ready — press Ctrl+C to stop")

    # 10. Run Flask-SocketIO (blocking)
    socketio.run(app, host=_SVC.HOST, port=_SVC.PORT, allow_unsafe_werkzeug=True)