import 'dart:async';
import 'package:socket_io_client/socket_io_client.dart' as IO;
import 'api_service.dart';

// ============================================================
// Opt #28 — Typed SocketIO event bus
// ============================================================
// Each server event is a broadcast StreamController.
// Widgets subscribe in initState() and cancel in dispose().
//
// Old pattern (callback hell, re-registration on every navigation):
//   _socket.connect(onScanStatus: (d) {...}, onDepositResult: (d) {...}, ...)
//
// New pattern (clean stream subscriptions):
//   _subs.add(_socket.onScanStatus.listen(_handleScan));
//   _subs.add(_socket.onDepositResult.listen(_handleDeposit));
//   // in dispose():
//   for (final s in _subs) s.cancel();
//
// connect() is idempotent — safe to call from multiple widgets.
// clearDvwCallbacks() / clearAdminCallbacks() are kept as no-ops for
// backward-compat during any partial migration.
// ============================================================

class SocketService {
  static final SocketService _instance = SocketService._internal();
  factory SocketService() => _instance;
  SocketService._internal();

  IO.Socket? socket;
  bool isConnected   = false;
  bool _isConnecting = false;
  // F5: request full alarm status only on the very first connect.
  // On reconnects the server will replay any active alarm via alarm_triggered;
  // re-requesting would race with live events and may push a stale snapshot.
  bool _firstConnect = true;

  // ── Typed broadcast streams ───────────────────────────────────────────────
  // Broadcast: multiple widgets can subscribe simultaneously.
  // Each StreamController is permanent for the lifetime of the singleton —
  // do NOT close them; widgets cancel their own subscriptions instead.

  // Scan
  final _scanStatus             = StreamController<Map<String, dynamic>>.broadcast();

  // DVW operations
  final _depositWaiting         = StreamController<Map<String, dynamic>>.broadcast();
  final _depositResult          = StreamController<Map<String, dynamic>>.broadcast();
  final _withdrawWaiting        = StreamController<Map<String, dynamic>>.broadcast();
  final _withdrawResult         = StreamController<Map<String, dynamic>>.broadcast();
  final _verifyWaiting          = StreamController<Map<String, dynamic>>.broadcast();
  final _verifyResult           = StreamController<Map<String, dynamic>>.broadcast();
  final _operationError         = StreamController<Map<String, dynamic>>.broadcast();
  final _operationCancelled     = StreamController<Map<String, dynamic>>.broadcast();

  // Phone tracking
  final _trackingStarted        = StreamController<Map<String, dynamic>>.broadcast();
  final _trackingUpdate         = StreamController<Map<String, dynamic>>.broadcast();
  final _trackingFailed         = StreamController<Map<String, dynamic>>.broadcast();

  // Alarm
  final _alarmTriggered         = StreamController<Map<String, dynamic>>.broadcast();
  final _alarmUpdated           = StreamController<Map<String, dynamic>>.broadcast();
  final _alarmCleared           = StreamController<Map<String, dynamic>>.broadcast();
  final _alarmAcknowledgeResult = StreamController<Map<String, dynamic>>.broadcast();
  final _alarmStatus            = StreamController<Map<String, dynamic>>.broadcast();

  // Admin resolution session
  final _adminSessionOpened     = StreamController<Map<String, dynamic>>.broadcast();
  final _adminSessionError      = StreamController<Map<String, dynamic>>.broadcast();
  final _adminRemoveOk          = StreamController<Map<String, dynamic>>.broadcast();
  final _adminQrResult          = StreamController<Map<String, dynamic>>.broadcast();
  final _adminNoQrResult        = StreamController<Map<String, dynamic>>.broadcast();
  final _adminStageOk           = StreamController<Map<String, dynamic>>.broadcast();
  final _adminUnstageOk         = StreamController<Map<String, dynamic>>.broadcast();
  final _adminAutoStaged        = StreamController<Map<String, dynamic>>.broadcast();
  final _adminPlaceResult       = StreamController<Map<String, dynamic>>.broadcast();
  final _adminMissingResult     = StreamController<Map<String, dynamic>>.broadcast();
  final _adminSessionClosed     = StreamController<Map<String, dynamic>>.broadcast();
  final _adminOperationError    = StreamController<Map<String, dynamic>>.broadcast();
  final _adminStepCancelled     = StreamController<Map<String, dynamic>>.broadcast();

  // ── Public stream getters ─────────────────────────────────────────────────

  // Scan
  Stream<Map<String, dynamic>> get onScanStatus             => _scanStatus.stream;

  // DVW
  Stream<Map<String, dynamic>> get onDepositWaiting         => _depositWaiting.stream;
  Stream<Map<String, dynamic>> get onDepositResult          => _depositResult.stream;
  Stream<Map<String, dynamic>> get onWithdrawWaiting        => _withdrawWaiting.stream;
  Stream<Map<String, dynamic>> get onWithdrawResult         => _withdrawResult.stream;
  Stream<Map<String, dynamic>> get onVerifyWaiting          => _verifyWaiting.stream;
  Stream<Map<String, dynamic>> get onVerifyResult           => _verifyResult.stream;
  Stream<Map<String, dynamic>> get onOperationError         => _operationError.stream;
  Stream<Map<String, dynamic>> get onOperationCancelled     => _operationCancelled.stream;

  // Tracking
  Stream<Map<String, dynamic>> get onTrackingStarted        => _trackingStarted.stream;
  Stream<Map<String, dynamic>> get onTrackingUpdate         => _trackingUpdate.stream;
  Stream<Map<String, dynamic>> get onTrackingFailed         => _trackingFailed.stream;

  // Alarm
  Stream<Map<String, dynamic>> get onAlarmTriggered         => _alarmTriggered.stream;
  Stream<Map<String, dynamic>> get onAlarmUpdated           => _alarmUpdated.stream;
  Stream<Map<String, dynamic>> get onAlarmCleared           => _alarmCleared.stream;
  Stream<Map<String, dynamic>> get onAlarmAcknowledgeResult => _alarmAcknowledgeResult.stream;
  Stream<Map<String, dynamic>> get onAlarmStatus            => _alarmStatus.stream;

  // Admin
  Stream<Map<String, dynamic>> get onAdminSessionOpened     => _adminSessionOpened.stream;
  Stream<Map<String, dynamic>> get onAdminSessionError      => _adminSessionError.stream;
  Stream<Map<String, dynamic>> get onAdminRemoveOk          => _adminRemoveOk.stream;
  Stream<Map<String, dynamic>> get onAdminQrResult          => _adminQrResult.stream;
  Stream<Map<String, dynamic>> get onAdminNoQrResult        => _adminNoQrResult.stream;
  Stream<Map<String, dynamic>> get onAdminStageOk           => _adminStageOk.stream;
  Stream<Map<String, dynamic>> get onAdminUnstageOk         => _adminUnstageOk.stream;
  Stream<Map<String, dynamic>> get onAdminAutoStaged        => _adminAutoStaged.stream;
  Stream<Map<String, dynamic>> get onAdminPlaceResult       => _adminPlaceResult.stream;
  Stream<Map<String, dynamic>> get onAdminMissingResult     => _adminMissingResult.stream;
  Stream<Map<String, dynamic>> get onAdminSessionClosed     => _adminSessionClosed.stream;
  Stream<Map<String, dynamic>> get onAdminOperationError    => _adminOperationError.stream;
  Stream<Map<String, dynamic>> get onAdminStepCancelled     => _adminStepCancelled.stream;

  // ── Connection ────────────────────────────────────────────────────────────

  /// Connect to the server. Idempotent — safe to call from any widget.
  void connect() {
    if (isConnected || _isConnecting) return;
    _isConnecting = true;

    // A3 (extended): same dart-define as ApiService.baseUrl so both HTTP and
    // WebSocket always point at the same server.
    // Dev:        flutter run                                    → localhost:5000
    // Production: flutter build apk --dart-define=API_BASE_URL=http://192.168.x.x:5000
    socket = IO.io(
      ApiService.baseUrl,
      <String, dynamic>{
        'transports': ['websocket'],
        'autoConnect': true,
      },
    );

    socket!.onConnect((_) {
      isConnected   = true;
      _isConnecting = false;
      requestStatus();
      // F5: alarm status only needed on first connect; on reconnect the server
      // re-sends alarm_triggered automatically if an alarm is still active.
      if (_firstConnect) {
        _firstConnect = false;
        requestAlarmStatus();
      }
    });

    socket!.onDisconnect((_) {
      isConnected   = false;
      _isConnecting = false;
    });

    _registerListeners();
  }

  // ── Type-safe data coercion ───────────────────────────────────────────────
  // socket_io_client delivers events as dynamic; coerce to typed Map.

  static Map<String, dynamic> _m(dynamic d) {
    if (d is Map<String, dynamic>) return d;
    if (d is Map) return Map<String, dynamic>.from(d);
    return <String, dynamic>{};
  }

  // ── Socket event → stream dispatch ───────────────────────────────────────
  // All server events are dispatched through broadcast streams.
  // No callback storage — widgets manage their own subscriptions.

  void _registerListeners() {
    if (socket == null) return;

    // Scan
    socket!.on('scan_status',               (d) => _scanStatus.add(_m(d)));

    // DVW operations
    socket!.on('deposit_waiting_for_qr',      (d) => _depositWaiting.add(_m(d)));
    socket!.on('deposit_result',              (d) => _depositResult.add(_m(d)));
    socket!.on('withdraw_waiting_for_action', (d) => _withdrawWaiting.add(_m(d)));
    socket!.on('withdraw_result',             (d) => _withdrawResult.add(_m(d)));
    socket!.on('verify_waiting_for_action',   (d) => _verifyWaiting.add(_m(d)));
    socket!.on('verify_result',               (d) => _verifyResult.add(_m(d)));
    socket!.on('operation_error',             (d) => _operationError.add(_m(d)));
    socket!.on('operation_cancelled',         (d) => _operationCancelled.add(_m(d)));

    // Phone tracking
    socket!.on('tracking_started', (d) => _trackingStarted.add(_m(d)));
    socket!.on('tracking_update',  (d) => _trackingUpdate.add(_m(d)));
    socket!.on('tracking_failed',  (d) => _trackingFailed.add(_m(d)));

    // Alarm
    socket!.on('alarm_triggered',          (d) => _alarmTriggered.add(_m(d)));
    socket!.on('alarm_updated',            (d) => _alarmUpdated.add(_m(d)));
    socket!.on('alarm_cleared',            (d) => _alarmCleared.add(_m(d)));
    socket!.on('alarm_acknowledge_result', (d) => _alarmAcknowledgeResult.add(_m(d)));
    socket!.on('alarm_status',             (d) => _alarmStatus.add(_m(d)));

    // Admin resolution
    socket!.on('admin_session_opened',  (d) => _adminSessionOpened.add(_m(d)));
    socket!.on('admin_session_error',   (d) => _adminSessionError.add(_m(d)));
    socket!.on('admin_remove_ok',       (d) => _adminRemoveOk.add(_m(d)));
    socket!.on('admin_qr_result',       (d) => _adminQrResult.add(_m(d)));
    socket!.on('admin_no_qr_result',    (d) => _adminNoQrResult.add(_m(d)));
    socket!.on('admin_stage_ok',        (d) => _adminStageOk.add(_m(d)));
    socket!.on('admin_unstage_ok',      (d) => _adminUnstageOk.add(_m(d)));
    socket!.on('admin_auto_staged',     (d) => _adminAutoStaged.add(_m(d)));
    socket!.on('admin_place_result',    (d) => _adminPlaceResult.add(_m(d)));
    socket!.on('admin_missing_result',  (d) => _adminMissingResult.add(_m(d)));
    socket!.on('admin_session_closed',  (d) => _adminSessionClosed.add(_m(d)));
    socket!.on('admin_operation_error', (d) => _adminOperationError.add(_m(d)));
    socket!.on('admin_step_cancelled',  (d) => _adminStepCancelled.add(_m(d)));
  }

  // ── Scan ──────────────────────────────────────────────────────────────────

  void toggleScan()    => _emit('toggle_scan', {'toggle': true});
  void requestStatus() => _emit('get_status', {});

  // ── DVW ───────────────────────────────────────────────────────────────────

  void deposit(String pid)  => _emit('deposit',  {'pid': pid});
  void withdraw(String pid) => _emit('withdraw', {'pid': pid});
  void qrScanned()          => _emit('qr_scanned', {});
  void cancelOperation()    => _emit('cancel_operation', {});

  void verify({required String pid, required int originalLid}) =>
      _emit('verify', {'pid': pid, 'original_lid': originalLid});

  // ── Alarm ─────────────────────────────────────────────────────────────────

  void acknowledgeAlarm(String password) =>
      _emit('alarm_acknowledge', {'password': password});
  void unsilenceAlarm()     => _emit('alarm_unsilence', {});
  void requestAlarmStatus() => _emit('get_alarm_status', {});

  // ── Admin resolution ──────────────────────────────────────────────────────

  void adminSessionStart(String password) =>
      _emit('admin_session_start', {'password': password});
  void adminRemovePhone(int fromLid) =>
      _emit('admin_remove_phone', {'from_lid': fromLid});
  void adminQrScanned()  => _emit('admin_qr_scanned', {});
  void adminNoQrFound()  => _emit('admin_no_qr_found', {});
  void adminStagePhone() => _emit('admin_stage_phone', {});
  void adminUnstagePhone(String pid) =>
      _emit('admin_unstage_phone', {'pid': pid});
  void adminPlacePhone(int toLid) =>
      _emit('admin_place_phone', {'to_lid': toLid});
  void adminDeclareMissing(String pid) =>
      _emit('admin_declare_missing', {'pid': pid});
  void adminSessionClose() => _emit('admin_session_close', {});
  void adminForceClose({bool safe = true}) =>
      _emit('admin_force_close_session', {'safe': safe});
  void adminCancelStep() => _emit('admin_cancel_step', {});

  // ── Teardown ──────────────────────────────────────────────────────────────

  void disconnect() {
    socket?.disconnect();
    socket?.dispose();
    socket        = null;
    isConnected   = false;
    _isConnecting = false;
  }

  // ── Internal ──────────────────────────────────────────────────────────────

  void _emit(String event, Map<String, dynamic> data) {
    if (!isConnected || socket == null) return;
    socket!.emit(event, data);
  }
}