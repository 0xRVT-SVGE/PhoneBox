# ============================================================
# FILE: back_end/server/evidence_storage.py
# ============================================================
"""
Opt #38 — Local Evidence Storage Manager
=========================================
Replaces ad-hoc evidence/ directory writes with a single managed store:

  • Date-partitioned layout:  evidence/YYYY-MM-DD/<session_id>/<clips>
  • Retention policy:         auto-delete sessions (kept=False) older than N days
  • Disk cap:                 warn + optional prune when usage > threshold
  • Thread-safe:              single lock for all mutations
  • REST-ready:               session/clip listing for Flutter evidence viewer

Config → back_end/config.py → EvidenceStorageConfig

Usage:
    from back_end.server.evidence_storage import evidence_store

    # Resolve (and create) a session directory
    session_dir = evidence_store.session_dir("abc123")

    # Register a closed session for retention tracking
    evidence_store.register_session(
        session_id="abc123",
        outcome="clean",       # "clean" | "flagged" | "force_closed"
        kept=False,
        clip_count=4,
        duration_s=87.3,
    )

    # List all sessions for Flutter evidence page
    sessions = evidence_store.list_sessions(kept_only=True)

    # Resolve a clip path (for Flask send_file)
    path = evidence_store.clip_path("abc123", "clip_filename.mp4")
"""

import json
import logging
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


# ── Config import with defaults ──────────────────────────────────────────────
try:
    from back_end.config import EvidenceStorageConfig as _ESC
    _BASE_DIR         = Path(_ESC.BASE_DIR)
    _RETENTION_DAYS   = _ESC.RETENTION_DAYS
    _DISK_WARN_GB     = _ESC.DISK_WARN_GB
    _DISK_HARD_CAP_GB = _ESC.DISK_HARD_CAP_GB
    _PRUNE_INTERVAL_S = _ESC.PRUNE_INTERVAL_S
except AttributeError:
    _BASE_DIR         = Path("evidence")
    _RETENTION_DAYS   = 30
    _DISK_WARN_GB     = 10.0
    _DISK_HARD_CAP_GB = 20.0
    _PRUNE_INTERVAL_S = 3600

# ── Session metadata ──────────────────────────────────────────────────────────
_META_FILE = "_meta.json"


def _meta_path(session_dir: Path) -> Path:
    return session_dir / _META_FILE


def _write_meta(session_dir: Path, meta: dict) -> None:
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        with open(_meta_path(session_dir), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
    except Exception as e:
        logger.warning(f"[EvidenceStore] Failed to write meta {session_dir}: {e}")


def _read_meta(session_dir: Path) -> Optional[dict]:
    p = _meta_path(session_dir)
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _sanitize(name: str) -> str:
    """Replace filesystem-unsafe characters."""
    return name.replace("/", "_").replace("\\", "_").replace(":", "_")[:64]


# ── Manager ───────────────────────────────────────────────────────────────────

class EvidenceStorageManager:
    """
    Thread-safe local evidence storage manager.

    Directory layout:
        evidence/
          YYYY-MM-DD/
            <session_id>/
              _meta.json          ← session metadata
              <safe_pid>_slot<N>_<ts>_live.mp4
              <safe_pid>_slot<N>_<ts>_pre.mp4
              …

    _meta.json schema:
        {
          "session_id":  "abc123",
          "date":        "2025-04-28",
          "opened_at":   "2025-04-28T14:00:00",
          "closed_at":   "2025-04-28T14:07:30",
          "outcome":     "clean" | "flagged" | "force_closed",
          "kept":        false,
          "clip_count":  4,
          "duration_s":  87.3
        }
    """

    def __init__(self):
        self._lock         = threading.Lock()
        self._base         = _BASE_DIR
        self._base.mkdir(parents=True, exist_ok=True)
        self._pruner:      Optional[threading.Thread] = None
        self._stop_pruner  = threading.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start background retention pruner. Call once at server startup."""
        if self._pruner is not None:
            return
        self._stop_pruner.clear()
        self._pruner = threading.Thread(
            target=self._prune_loop,
            daemon=True,
            name="EvidencePruner",
        )
        self._pruner.start()
        logger.info(
            f"[EvidenceStore] Started — base={self._base} "
            f"retention={_RETENTION_DAYS}d "
            f"warn={_DISK_WARN_GB}GB cap={_DISK_HARD_CAP_GB}GB"
        )

    def stop(self) -> None:
        self._stop_pruner.set()
        if self._pruner:
            self._pruner.join(timeout=3.0)
        self._pruner = None

    # ── Session directory resolution ──────────────────────────────────────────

    def session_dir(self, session_id: str, date: Optional[datetime] = None) -> Path:
        """
        Return the Path for a session, creating it on disk.
        Use this instead of constructing paths manually — guarantees the
        date-partitioned layout and thread-safe mkdir.
        """
        if date is None:
            date = datetime.utcnow()
        date_str = date.strftime("%Y-%m-%d")
        d = self._base / date_str / _sanitize(session_id)
        with self._lock:
            d.mkdir(parents=True, exist_ok=True)
        return d

    # ── Session registration ──────────────────────────────────────────────────

    def register_session(
        self,
        session_id: str,
        outcome: str,
        kept: bool,
        clip_count: int = 0,
        duration_s: float = 0.0,
        opened_at: Optional[datetime] = None,
    ) -> None:
        """
        Write _meta.json for a closed session.
        Called by EvidenceRecorder.close() automatically.
        """
        now = datetime.utcnow()
        d   = self._find_session_dir(session_id)
        if d is None:
            logger.debug(
                f"[EvidenceStore] register_session: dir not found for {session_id} "
                "(session may have been deleted — OK for clean outcomes)"
            )
            return

        meta = {
            "session_id": session_id,
            "date":       d.parent.name,
            "opened_at":  (opened_at or now).isoformat(),
            "closed_at":  now.isoformat(),
            "outcome":    outcome,
            "kept":       kept,
            "clip_count": clip_count,
            "duration_s": round(duration_s, 1),
        }
        _write_meta(d, meta)
        logger.info(
            f"[EvidenceStore] Registered: {session_id} "
            f"outcome={outcome} kept={kept} clips={clip_count}"
        )

    # ── Clip resolution ───────────────────────────────────────────────────────

    def clip_path(self, session_id: str, filename: str) -> Optional[Path]:
        """Return the full Path to a named clip file, or None if not found."""
        d = self._find_session_dir(session_id)
        if d is None:
            return None
        p = d / filename
        return p if p.exists() else None

    # ── Listing ───────────────────────────────────────────────────────────────

    def list_sessions(
        self,
        kept_only: bool = False,
        limit: int = 100,
    ) -> List[dict]:
        """
        Return session metadata sorted newest-first.
        Used by the Flutter evidence viewer REST endpoint.
        """
        results = []
        try:
            date_dirs = sorted(
                (d for d in self._base.iterdir() if d.is_dir() and not d.name.startswith("_")),
                reverse=True,
            )
        except OSError:
            return []

        for date_dir in date_dirs:
            for sess_dir in sorted(date_dir.iterdir(), reverse=True):
                if not sess_dir.is_dir():
                    continue
                meta = _read_meta(sess_dir) or self._infer_meta(sess_dir)
                if kept_only and not meta.get("kept", True):
                    continue
                clips = [
                    {"name": f.name, "size_mb": round(f.stat().st_size / 1_048_576, 2)}
                    for f in sorted(sess_dir.iterdir())
                    if f.is_file() and f.suffix in (".mp4", ".avi")
                ]
                results.append({**meta, "clips": clips})
                if len(results) >= limit:
                    return results

        return results

    def disk_usage_gb(self) -> float:
        """Return total evidence directory size in GB."""
        try:
            total = sum(
                f.stat().st_size
                for f in self._base.rglob("*")
                if f.is_file()
            )
            return total / 1_073_741_824
        except OSError:
            return 0.0

    # ── Retention ─────────────────────────────────────────────────────────────

    def prune_old_sessions(self, dry_run: bool = False) -> int:
        """
        Delete sessions (kept=False) older than RETENTION_DAYS.
        Returns count deleted.
        """
        cutoff  = datetime.utcnow() - timedelta(days=_RETENTION_DAYS)
        deleted = 0

        for date_dir in sorted(self._base.iterdir()):
            if not date_dir.is_dir():
                continue
            try:
                dir_date = datetime.strptime(date_dir.name, "%Y-%m-%d")
            except ValueError:
                continue
            if dir_date >= cutoff:
                continue

            for sess_dir in list(date_dir.iterdir()):
                if not sess_dir.is_dir():
                    continue
                meta = _read_meta(sess_dir)
                if meta and meta.get("kept", True):
                    continue   # never auto-delete kept evidence
                if not dry_run:
                    try:
                        shutil.rmtree(sess_dir)
                        logger.info(f"[EvidenceStore] Pruned: {sess_dir.name}")
                        deleted += 1
                    except Exception as e:
                        logger.warning(f"[EvidenceStore] Prune failed {sess_dir}: {e}")
                else:
                    deleted += 1

            # Remove empty date directory
            if not dry_run:
                try:
                    if date_dir.exists() and not any(date_dir.iterdir()):
                        date_dir.rmdir()
                except Exception:
                    pass

        return deleted

    def prune_to_cap(self, hard_cap_gb: float) -> int:
        """
        Enforce hard disk cap by deleting oldest non-kept sessions.
        """
        deleted = 0
        while self.disk_usage_gb() > hard_cap_gb:
            oldest: Optional[Path] = None
            oldest_dt: Optional[datetime] = None

            for date_dir in sorted(self._base.iterdir()):
                if not date_dir.is_dir():
                    continue
                try:
                    dir_date = datetime.strptime(date_dir.name, "%Y-%m-%d")
                except ValueError:
                    continue
                for sess_dir in sorted(date_dir.iterdir()):
                    if not sess_dir.is_dir():
                        continue
                    meta = _read_meta(sess_dir)
                    if meta and meta.get("kept", True):
                        continue
                    if oldest is None or dir_date < oldest_dt:
                        oldest    = sess_dir
                        oldest_dt = dir_date

            if oldest is None:
                logger.warning(
                    "[EvidenceStore] Hard-cap exceeded but all sessions are kept "
                    "— cannot auto-prune"
                )
                break

            try:
                shutil.rmtree(oldest)
                logger.warning(
                    f"[EvidenceStore] Hard-cap prune: {oldest.name} "
                    f"(usage={self.disk_usage_gb():.2f}GB cap={hard_cap_gb}GB)"
                )
                deleted += 1
            except Exception as e:
                logger.error(f"[EvidenceStore] Hard-cap prune failed: {e}")
                break

        return deleted

    # ── Background pruner loop ────────────────────────────────────────────────

    def _prune_loop(self) -> None:
        while not self._stop_pruner.wait(timeout=_PRUNE_INTERVAL_S):
            self._run_retention_check()

    def _run_retention_check(self) -> None:
        try:
            usage = self.disk_usage_gb()
            if usage > _DISK_HARD_CAP_GB:
                logger.warning(
                    f"[EvidenceStore] Hard cap: {usage:.2f}GB > {_DISK_HARD_CAP_GB}GB — pruning"
                )
                self.prune_to_cap(_DISK_HARD_CAP_GB)
            elif usage > _DISK_WARN_GB:
                logger.warning(
                    f"[EvidenceStore] Disk warn: {usage:.2f}GB > {_DISK_WARN_GB}GB"
                )
            n = self.prune_old_sessions()
            if n:
                logger.info(f"[EvidenceStore] Pruned {n} old session(s)")
        except Exception as e:
            logger.error(f"[EvidenceStore] Retention check failed: {e}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _find_session_dir(self, session_id: str) -> Optional[Path]:
        """Scan date dirs to locate a session by ID."""
        safe = _sanitize(session_id)
        try:
            for date_dir in self._base.iterdir():
                if not date_dir.is_dir():
                    continue
                candidate = date_dir / safe
                if candidate.is_dir():
                    return candidate
        except OSError:
            pass
        return None

    @staticmethod
    def _infer_meta(sess_dir: Path) -> dict:
        return {
            "session_id": sess_dir.name,
            "date":       sess_dir.parent.name,
            "opened_at":  None,
            "closed_at":  None,
            "outcome":    "unknown",
            "kept":       True,
            "clip_count": sum(1 for f in sess_dir.iterdir() if f.suffix == ".mp4"),
            "duration_s": 0.0,
        }


# ── Module-level singleton ────────────────────────────────────────────────────

evidence_store = EvidenceStorageManager()
"""
Import everywhere with:
    from back_end.server.evidence_storage import evidence_store
"""
