// ============================================================
// FILE: front_end/offline_op_queue.dart
// ============================================================
//
// Opt #33 — Offline-first operation queue (sqflite persistence).
//
// If the WebSocket connection drops while a user is mid-operation
// (depositing or withdrawing a phone) the operation is persisted
// locally and automatically replayed the next time the socket
// reconnects.
//
// This covers the most common failure mode on an intranet: a brief
// network hiccup between the kiosk and the server (< 30 s).  The
// user sees a clear "pending" indicator instead of a silent failure.
//
// Design
// ──────
//  • SQLite database created at first use (sqflite, local device).
//  • One row per pending operation: pid, op_type, enqueued_at.
//  • SocketService calls enqueue() when it cannot deliver an op.
//  • On every socket reconnect SocketService calls drainAndReplay().
//  • drainAndReplay() replays each op in insertion order, then
//    removes it from the queue regardless of outcome (the server
//    will return an error if the op is stale — UI handles it).
//  • At most MAX_QUEUED_OPS rows kept (oldest evicted on overflow).
//
// Intranet note
// ─────────────
//  sqflite writes to the device's app-data directory — zero network
//  dependency.  No cloud sync, no internet required.
//
// Usage
// ─────
//  // Singleton access:
//  final q = OfflineOpQueue.instance;
//  await q.enqueue(pid: pid, opType: 'deposit');
//
//  // On reconnect:
//  await q.drainAndReplay(socketService);
// ============================================================

import 'dart:async';
import 'package:flutter/foundation.dart';
import 'package:sqflite/sqflite.dart';
import 'package:path/path.dart' as p;
import 'socket_service.dart';

// ── Data model ────────────────────────────────────────────────────────────────

class PendingOp {
  final int    id;
  final String pid;
  final String opType;        // "deposit" | "withdraw"
  final DateTime enqueuedAt;

  const PendingOp({
    required this.id,
    required this.pid,
    required this.opType,
    required this.enqueuedAt,
  });

  factory PendingOp.fromMap(Map<String, dynamic> m) => PendingOp(
    id:          m['id']          as int,
    pid:         m['pid']         as String,
    opType:      m['op_type']     as String,
    enqueuedAt:  DateTime.fromMillisecondsSinceEpoch(m['enqueued_ms'] as int),
  );

  @override
  String toString() => 'PendingOp(#$id $opType pid=$pid)';
}

// ── Queue ─────────────────────────────────────────────────────────────────────

class OfflineOpQueue {

  OfflineOpQueue._();
  static final OfflineOpQueue instance = OfflineOpQueue._();

  static const _dbName     = 'phonebox_ops.db';
  static const _table      = 'pending_ops';
  static const _dbVersion  = 1;

  /// Maximum rows kept.  Oldest are evicted first to prevent unbounded growth.
  static const _maxRows    = 20;

  Database? _db;

  // ── Lifecycle ──────────────────────────────────────────────────────────────

  Future<void> open() async {
    if (_db != null) return;
    final dir  = await getDatabasesPath();
    final path = p.join(dir, _dbName);
    _db = await openDatabase(
      path,
      version: _dbVersion,
      onCreate: (db, _) async {
        await db.execute('''
          CREATE TABLE $_table (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            pid          TEXT    NOT NULL,
            op_type      TEXT    NOT NULL,
            enqueued_ms  INTEGER NOT NULL
          )
        ''');
      },
    );
  }

  Future<void> close() async {
    await _db?.close();
    _db = null;
  }

  // ── Write ──────────────────────────────────────────────────────────────────

  /// Persist an operation that could not be sent.
  /// Evicts the oldest row if _maxRows would be exceeded.
  Future<void> enqueue({required String pid, required String opType}) async {
    await open();
    final db = _db!;

    await db.transaction((txn) async {
      await txn.insert(_table, {
        'pid':         pid,
        'op_type':     opType,
        'enqueued_ms': DateTime.now().millisecondsSinceEpoch,
      });

      // Evict oldest rows beyond cap
      final count = Sqflite.firstIntValue(
        await txn.rawQuery('SELECT COUNT(*) FROM $_table'),
      ) ?? 0;
      if (count > _maxRows) {
        await txn.rawDelete('''
          DELETE FROM $_table WHERE id IN (
            SELECT id FROM $_table ORDER BY id ASC LIMIT ?
          )
        ''', [count - _maxRows]);
      }
    });

    debugPrint('[OfflineOpQueue] queued: $opType / $pid');
  }

  // ── Read ───────────────────────────────────────────────────────────────────

  /// Return all pending operations ordered by insertion time (oldest first).
  Future<List<PendingOp>> pendingOps() async {
    await open();
    final rows = await _db!.query(_table, orderBy: 'id ASC');
    return rows.map(PendingOp.fromMap).toList();
  }

  Future<int> pendingCount() async {
    await open();
    return Sqflite.firstIntValue(
      await _db!.rawQuery('SELECT COUNT(*) FROM $_table'),
    ) ?? 0;
  }

  // ── Delete ─────────────────────────────────────────────────────────────────

  Future<void> _remove(int id) async {
    await _db!.delete(_table, where: 'id = ?', whereArgs: [id]);
  }

  Future<void> clearAll() async {
    await open();
    await _db!.delete(_table);
    debugPrint('[OfflineOpQueue] cleared all pending ops');
  }

  // ── Replay ─────────────────────────────────────────────────────────────────

  /// Replay all pending operations via [socketService].
  ///
  /// Returns immediately with 0 if the socket is not connected — ops stay in
  /// the queue and will be retried on the next call (i.e. the next page load
  /// after reconnection).
  ///
  /// Each op is sent and then removed from the queue inside the same try block.
  /// If the emit itself throws (shouldn't happen but defensive), the op is kept.
  ///
  /// Returns the number of ops successfully sent and removed.
  Future<int> drainAndReplay(SocketService socketService) async {
    // Guard: don't drain if socket isn't connected — _emit silently drops.
    if (!socketService.isConnected) {
      debugPrint('[OfflineOpQueue] socket not connected — skipping drain');
      return 0;
    }

    final ops = await pendingOps();
    if (ops.isEmpty) return 0;

    debugPrint('[OfflineOpQueue] replaying ${ops.length} pending op(s)');

    int replayed = 0;
    for (final op in ops) {
      try {
        if (op.opType == 'deposit') {
          socketService.deposit(op.pid);
        } else if (op.opType == 'withdraw') {
          socketService.withdraw(op.pid);
        } else {
          debugPrint('[OfflineOpQueue] unknown op type "${op.opType}" — removing');
        }
        // Only remove AFTER a successful emit (emit is sync, no throw expected)
        await _remove(op.id);
        replayed++;
        debugPrint('[OfflineOpQueue] replayed: $op');
      } catch (e) {
        // Keep the op in the queue — will retry later
        debugPrint('[OfflineOpQueue] replay error for $op: $e');
      }
    }
    return replayed;
  }

}
