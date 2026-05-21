# ============================================================
# FILE: back_end/server/metrics.py
# ============================================================
"""
Opt #41 — Prometheus metrics for PhoneBox.

Exposes a /metrics endpoint consumed by Prometheus.
Grafana reads from Prometheus for dashboards.

Metrics exposed
───────────────
  phonebox_frames_processed_total{worker}  — per-worker frame count
  phonebox_avg_embedding_distance{worker}  — rolling avg cosine distance
  phonebox_alarms_triggered_total          — cumulative alarm fires
  phonebox_alarms_suppressed_total         — suppressed (DVW active)
  phonebox_baselines_adapted_total{worker} — soft baseline updates
  phonebox_worker_errors_total{worker}     — worker exceptions
  phonebox_active_mismatches               — current alarm mismatch count
  phonebox_alarm_active                    — 1 if alarm on, 0 if off
  phonebox_dvw_operations_active           — in-flight DVW operations
  phonebox_encoder_queue_depth             — BackgroundEncoder queue length
  phonebox_encoder_dropped_total           — clips dropped due to full queue
  phonebox_top_camera_frames_total         — frames captured by top camera
  phonebox_bottom_camera_frames_total      — frames from async bottom cam

Install
───────
  pip install prometheus_client

Grafana quickstart (Docker)
───────────────────────────
  # prometheus.yml (save alongside docker-compose.yml):
  #   global:
  #     scrape_interval: 5s
  #   scrape_configs:
  #     - job_name: phonebox
  #       static_configs:
  #         - targets: ['host.docker.internal:5000']
  #       metrics_path: /metrics

  docker run -d -p 9090:9090 \\
    -v $(pwd)/prometheus.yml:/etc/prometheus/prometheus.yml \\
    prom/prometheus

  docker run -d -p 3000:3000 grafana/grafana

  # In Grafana → Add data source → Prometheus → http://localhost:9090
"""

import logging
import time
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

# ── prometheus_client availability ───────────────────────────────────────────
try:
    from prometheus_client import (
        Counter, Gauge, Histogram, generate_latest,
        CollectorRegistry, CONTENT_TYPE_LATEST,
    )
    _PROM_AVAILABLE = True
except ImportError:
    _PROM_AVAILABLE = False
    logger.warning(
        "[Metrics] prometheus_client not installed — /metrics endpoint disabled. "
        "Install with: pip install prometheus_client"
    )

if TYPE_CHECKING:
    from flask import Flask

# ── Registry & metric definitions ────────────────────────────────────────────

if _PROM_AVAILABLE:
    _REG = CollectorRegistry(auto_describe=True)

    # Worker-level counters / gauges
    _frames_total = Counter(
        'phonebox_frames_processed_total',
        'Total frames processed per slot-monitor worker',
        ['worker'], registry=_REG,
    )
    _avg_distance = Gauge(
        'phonebox_avg_embedding_distance',
        'Rolling average cosine embedding distance per worker',
        ['worker'], registry=_REG,
    )
    _alarms_triggered = Counter(
        'phonebox_alarms_triggered_total',
        'Cumulative slot-mismatch alarms fired',
        registry=_REG,
    )
    _alarms_suppressed = Counter(
        'phonebox_alarms_suppressed_total',
        'Alarms suppressed because a DVW/admin operation was active',
        registry=_REG,
    )
    _baselines_adapted = Counter(
        'phonebox_baselines_adapted_total',
        'Soft baseline recalibrations per worker',
        ['worker'], registry=_REG,
    )
    _worker_errors = Counter(
        'phonebox_worker_errors_total',
        'Unhandled exceptions inside slot-monitor workers',
        ['worker'], registry=_REG,
    )

    # System-level gauges
    _active_mismatches = Gauge(
        'phonebox_active_mismatches',
        'Number of active slot mismatches currently tracked by AlarmController',
        registry=_REG,
    )
    _alarm_active = Gauge(
        'phonebox_alarm_active',
        '1 if alarm is currently active, 0 otherwise',
        registry=_REG,
    )
    _dvw_operations_active = Gauge(
        'phonebox_dvw_operations_active',
        'Number of in-flight Deposit/Withdraw/Verify operations',
        registry=_REG,
    )

    # Background encoder
    _encoder_queue_depth = Gauge(
        'phonebox_encoder_queue_depth',
        'Current depth of the BackgroundEncoder job queue',
        registry=_REG,
    )
    _encoder_dropped = Counter(
        'phonebox_encoder_dropped_total',
        'Evidence clips dropped because the encoder queue was full',
        registry=_REG,
    )

    # Camera frame counters
    _top_cam_frames = Counter(
        'phonebox_top_camera_frames_total',
        'Total frames captured by the top-down camera',
        registry=_REG,
    )
    _bottom_cam_frames = Counter(
        'phonebox_bottom_camera_frames_total',
        'Total frames captured by the async bottom camera',
        registry=_REG,
    )

    # Scrape performance
    _scrape_duration = Histogram(
        'phonebox_metrics_scrape_duration_seconds',
        'Time taken to collect all metrics for one /metrics scrape',
        registry=_REG,
    )

    # Internal: track previous counter values so we can compute deltas
    # (prometheus_client Counter only increments; we store last-seen worker
    # metrics to compute the increment each scrape)
    _prev_worker_frames:     dict = {}
    _prev_worker_alarms:     dict = {}
    _prev_worker_suppressed: dict = {}
    _prev_worker_baselines:  dict = {}
    _prev_worker_errors:     dict = {}
    _prev_encoder_dropped:   int  = 0
    _prev_top_frames:        int  = 0
    _prev_bottom_frames:     int  = 0


# ── Collection helpers ────────────────────────────────────────────────────────

def _collect_worker_metrics(slot_monitor) -> None:
    """Pull metrics from HeadlessSlotMonitor.worker_pool."""
    global _prev_worker_frames, _prev_worker_alarms, _prev_worker_suppressed
    global _prev_worker_baselines, _prev_worker_errors

    if slot_monitor is None:
        return
    wp = getattr(slot_monitor, 'worker_pool', None)
    if wp is None:
        return

    try:
        pool_metrics = wp.get_metrics()
    except Exception as exc:
        logger.debug(f"[Metrics] worker_pool.get_metrics() failed: {exc}")
        return

    for wm in pool_metrics.get('workers', []):
        wid = str(wm['worker_id'])

        # frames
        prev_f = _prev_worker_frames.get(wid, 0)
        curr_f = wm['frames_processed']
        if curr_f > prev_f:
            _frames_total.labels(worker=wid).inc(curr_f - prev_f)
        _prev_worker_frames[wid] = curr_f

        # avg distance (gauge — just set)
        _avg_distance.labels(worker=wid).set(wm['avg_distance'])

        # alarms triggered
        prev_a = _prev_worker_alarms.get(wid, 0)
        curr_a = wm['alarms_triggered']
        if curr_a > prev_a:
            _alarms_triggered.inc(curr_a - prev_a)
        _prev_worker_alarms[wid] = curr_a

        # alarms suppressed
        prev_s = _prev_worker_suppressed.get(wid, 0)
        curr_s = wm['alarms_suppressed']
        if curr_s > prev_s:
            _alarms_suppressed.inc(curr_s - prev_s)
        _prev_worker_suppressed[wid] = curr_s

        # baselines adapted
        prev_b = _prev_worker_baselines.get(wid, 0)
        curr_b = wm['baselines_adapted']
        if curr_b > prev_b:
            _baselines_adapted.labels(worker=wid).inc(curr_b - prev_b)
        _prev_worker_baselines[wid] = curr_b

        # errors
        prev_e = _prev_worker_errors.get(wid, 0)
        curr_e = wm['errors']
        if curr_e > prev_e:
            _worker_errors.labels(worker=wid).inc(curr_e - prev_e)
        _prev_worker_errors[wid] = curr_e


def _collect_alarm_metrics(slot_monitor) -> None:
    """Pull mismatch count and alarm-active flag from AlarmController."""
    if slot_monitor is None:
        return
    alarm = getattr(slot_monitor, 'alarm', None)
    if alarm is None:
        return
    try:
        status = alarm.get_status()
        _active_mismatches.set(status.get('mismatch_count', 0))
        _alarm_active.set(1 if status.get('active') else 0)
    except Exception as exc:
        logger.debug(f"[Metrics] alarm.get_status() failed: {exc}")


def _collect_dvw_metrics() -> None:
    """Count active DVW operations from op_ctx."""
    try:
        from Backup.back_end.slot_monitor.services.operation_context import op_ctx
        ops = op_ctx.get_all_operations()
        _dvw_operations_active.set(len(ops))
    except Exception as exc:
        logger.debug(f"[Metrics] op_ctx metrics failed: {exc}")


def _collect_encoder_metrics() -> None:
    """Pull queue depth and dropped count from BackgroundEncoder."""
    global _prev_encoder_dropped
    try:
        from Backup.back_end.slot_monitor.admin.background_encoder import BackgroundEncoder
        st = BackgroundEncoder.instance().status()
        _encoder_queue_depth.set(st.get('queued', 0))
        curr_d = st.get('dropped', 0)
        if curr_d > _prev_encoder_dropped:
            _encoder_dropped.inc(curr_d - _prev_encoder_dropped)
        _prev_encoder_dropped = curr_d
    except Exception as exc:
        logger.debug(f"[Metrics] encoder metrics failed: {exc}")


def _collect_camera_metrics(slot_monitor) -> None:
    """Pull frame counts from top_camera and async bottom cam."""
    global _prev_top_frames, _prev_bottom_frames

    # Top camera — uses a simple frame event counter stored on top_camera
    try:
        from Backup.back_end.slot_monitor.camera.top_camera import top_camera
        # top_camera doesn't expose a frame count directly; we proxy via
        # rolling buffer which stores timestamps.  Use buffer frame_count()
        # as an approximation of total delivered frames since last scrape.
        from Backup.back_end.slot_monitor.camera.rolling_buffer import top_rolling_buffer
        curr_top = top_rolling_buffer.frame_count()
        if curr_top > _prev_top_frames:
            _top_cam_frames.inc(curr_top - _prev_top_frames)
        _prev_top_frames = curr_top
    except Exception as exc:
        logger.debug(f"[Metrics] top cam metrics failed: {exc}")

    # Bottom / async camera
    try:
        if slot_monitor is not None:
            fb = getattr(slot_monitor, 'frame_buffer', None)
            if fb is not None:
                curr_bottom = fb.get_frame_count()
                if curr_bottom > _prev_bottom_frames:
                    _bottom_cam_frames.inc(curr_bottom - _prev_bottom_frames)
                _prev_bottom_frames = curr_bottom
    except Exception as exc:
        logger.debug(f"[Metrics] bottom cam metrics failed: {exc}")


# ── Public: register Flask endpoint ──────────────────────────────────────────

def register_metrics_endpoint(app: 'Flask', get_slot_monitor_fn) -> None:
    """
    Register GET /metrics on *app*.

    Args:
        app:                  Flask application.
        get_slot_monitor_fn:  Zero-argument callable returning the current
                              HeadlessSlotMonitor instance (or None).
                              Typically server.app.get_slot_monitor.
    """
    if not _PROM_AVAILABLE:
        logger.warning("[Metrics] /metrics endpoint NOT registered (prometheus_client missing)")
        return

    from flask import Response

    @app.route('/metrics', methods=['GET'])
    def metrics_endpoint():
        t0 = time.perf_counter()
        monitor = get_slot_monitor_fn()
        _collect_worker_metrics(monitor)
        _collect_alarm_metrics(monitor)
        _collect_dvw_metrics()
        _collect_encoder_metrics()
        _collect_camera_metrics(monitor)
        _scrape_duration.observe(time.perf_counter() - t0)
        return Response(
            generate_latest(_REG),
            status=200,
            mimetype=CONTENT_TYPE_LATEST,
        )

    logger.info("[Metrics] Opt #41: /metrics endpoint registered (Prometheus scrape target)")