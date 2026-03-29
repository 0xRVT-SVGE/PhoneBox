import 'package:socket_io_client/socket_io_client.dart' as IO;

class SocketService {
  static final SocketService _instance = SocketService._internal();
  factory SocketService() => _instance;
  SocketService._internal();

  IO.Socket? socket;
  bool isConnected   = false;
  bool _isConnecting = false;

  // ── Scan ──────────────────────────────────────────────
  Function(dynamic)? _onScanStatus;

  // ── DVW operations ────────────────────────────────────
  Function(dynamic)? _onDepositWaiting;
  Function(dynamic)? _onDepositResult;
  Function(dynamic)? _onWithdrawWaiting;
  Function(dynamic)? _onWithdrawResult;
  Function(dynamic)? _onVerifyWaiting;
  Function(dynamic)? _onVerifyResult;
  Function(dynamic)? _onOperationError;
  Function(dynamic)? _onOperationCancelled;

  // ── Phone tracking ────────────────────────────────────
  Function(dynamic)? _onTrackingStarted;
  Function(dynamic)? _onTrackingUpdate;
  Function(dynamic)? _onTrackingFailed;

  // ── Alarm ─────────────────────────────────────────────
  Function(dynamic)? _onAlarmTriggered;
  Function(dynamic)? _onAlarmUpdated;
  Function(dynamic)? _onAlarmCleared;
  Function(dynamic)? _onAlarmAcknowledgeResult;
  Function(dynamic)? _onAlarmStatus;

  // ── Admin resolution session ──────────────────────────
  Function(dynamic)? _onAdminSessionOpened;
  Function(dynamic)? _onAdminSessionError;
  Function(dynamic)? _onAdminRemoveOk;
  Function(dynamic)? _onAdminQrResult;
  Function(dynamic)? _onAdminNoQrResult;
  Function(dynamic)? _onAdminStageOk;
  Function(dynamic)? _onAdminUnstageOk;
  Function(dynamic)? _onAdminPlaceResult;
  Function(dynamic)? _onAdminMissingResult;
  Function(dynamic)? _onAdminSessionClosed;
  Function(dynamic)? _onAdminOperationError;

  // ── Connect / callback registration ───────────────────

  void connect({
    Function(dynamic)? onScanStatus,
    Function(dynamic)? onDepositWaiting,
    Function(dynamic)? onDepositResult,
    Function(dynamic)? onWithdrawWaiting,
    Function(dynamic)? onWithdrawResult,
    Function(dynamic)? onVerifyWaiting,
    Function(dynamic)? onVerifyResult,
    Function(dynamic)? onOperationError,
    Function(dynamic)? onOperationCancelled,
    Function(dynamic)? onTrackingStarted,
    Function(dynamic)? onTrackingUpdate,
    Function(dynamic)? onTrackingFailed,
    Function(dynamic)? onAlarmTriggered,
    Function(dynamic)? onAlarmUpdated,
    Function(dynamic)? onAlarmCleared,
    Function(dynamic)? onAlarmAcknowledgeResult,
    Function(dynamic)? onAlarmStatus,
    Function(dynamic)? onAdminSessionOpened,
    Function(dynamic)? onAdminSessionError,
    Function(dynamic)? onAdminRemoveOk,
    Function(dynamic)? onAdminQrResult,
    Function(dynamic)? onAdminNoQrResult,
    Function(dynamic)? onAdminStageOk,
    Function(dynamic)? onAdminUnstageOk,
    Function(dynamic)? onAdminPlaceResult,
    Function(dynamic)? onAdminMissingResult,
    Function(dynamic)? onAdminSessionClosed,
    Function(dynamic)? onAdminOperationError,
  }) {
    _updateCallbacks(
      onScanStatus:             onScanStatus,
      onDepositWaiting:         onDepositWaiting,
      onDepositResult:          onDepositResult,
      onWithdrawWaiting:        onWithdrawWaiting,
      onWithdrawResult:         onWithdrawResult,
      onVerifyWaiting:          onVerifyWaiting,
      onVerifyResult:           onVerifyResult,
      onOperationError:         onOperationError,
      onOperationCancelled:     onOperationCancelled,
      onTrackingStarted:        onTrackingStarted,
      onTrackingUpdate:         onTrackingUpdate,
      onTrackingFailed:         onTrackingFailed,
      onAlarmTriggered:         onAlarmTriggered,
      onAlarmUpdated:           onAlarmUpdated,
      onAlarmCleared:           onAlarmCleared,
      onAlarmAcknowledgeResult: onAlarmAcknowledgeResult,
      onAlarmStatus:            onAlarmStatus,
      onAdminSessionOpened:     onAdminSessionOpened,
      onAdminSessionError:      onAdminSessionError,
      onAdminRemoveOk:          onAdminRemoveOk,
      onAdminQrResult:          onAdminQrResult,
      onAdminNoQrResult:        onAdminNoQrResult,
      onAdminStageOk:           onAdminStageOk,
      onAdminUnstageOk:         onAdminUnstageOk,
      onAdminPlaceResult:       onAdminPlaceResult,
      onAdminMissingResult:     onAdminMissingResult,
      onAdminSessionClosed:     onAdminSessionClosed,
      onAdminOperationError:    onAdminOperationError,
    );

    if (isConnected || _isConnecting) return;
    _isConnecting = true;

    socket = IO.io(
      "http://localhost:5000",
      <String, dynamic>{
        "transports": ["websocket"],
        "autoConnect": true,
      },
    );

    socket!.onConnect((_) {
      isConnected   = true;
      _isConnecting = false;
      requestStatus();
      requestAlarmStatus();
    });

    socket!.onDisconnect((_) {
      isConnected   = false;
      _isConnecting = false;
    });

    _registerListeners();
  }

  void _updateCallbacks({
    Function(dynamic)? onScanStatus,
    Function(dynamic)? onDepositWaiting,
    Function(dynamic)? onDepositResult,
    Function(dynamic)? onWithdrawWaiting,
    Function(dynamic)? onWithdrawResult,
    Function(dynamic)? onVerifyWaiting,
    Function(dynamic)? onVerifyResult,
    Function(dynamic)? onOperationError,
    Function(dynamic)? onOperationCancelled,
    Function(dynamic)? onTrackingStarted,
    Function(dynamic)? onTrackingUpdate,
    Function(dynamic)? onTrackingFailed,
    Function(dynamic)? onAlarmTriggered,
    Function(dynamic)? onAlarmUpdated,
    Function(dynamic)? onAlarmCleared,
    Function(dynamic)? onAlarmAcknowledgeResult,
    Function(dynamic)? onAlarmStatus,
    Function(dynamic)? onAdminSessionOpened,
    Function(dynamic)? onAdminSessionError,
    Function(dynamic)? onAdminRemoveOk,
    Function(dynamic)? onAdminQrResult,
    Function(dynamic)? onAdminNoQrResult,
    Function(dynamic)? onAdminStageOk,
    Function(dynamic)? onAdminUnstageOk,
    Function(dynamic)? onAdminPlaceResult,
    Function(dynamic)? onAdminMissingResult,
    Function(dynamic)? onAdminSessionClosed,
    Function(dynamic)? onAdminOperationError,
  }) {
    if (onScanStatus             != null) _onScanStatus             = onScanStatus;
    if (onDepositWaiting         != null) _onDepositWaiting         = onDepositWaiting;
    if (onDepositResult          != null) _onDepositResult          = onDepositResult;
    if (onWithdrawWaiting        != null) _onWithdrawWaiting        = onWithdrawWaiting;
    if (onWithdrawResult         != null) _onWithdrawResult         = onWithdrawResult;
    if (onVerifyWaiting          != null) _onVerifyWaiting          = onVerifyWaiting;
    if (onVerifyResult           != null) _onVerifyResult           = onVerifyResult;
    if (onOperationError         != null) _onOperationError         = onOperationError;
    if (onOperationCancelled     != null) _onOperationCancelled     = onOperationCancelled;
    if (onTrackingStarted        != null) _onTrackingStarted        = onTrackingStarted;
    if (onTrackingUpdate         != null) _onTrackingUpdate         = onTrackingUpdate;
    if (onTrackingFailed         != null) _onTrackingFailed         = onTrackingFailed;
    if (onAlarmTriggered         != null) _onAlarmTriggered         = onAlarmTriggered;
    if (onAlarmUpdated           != null) _onAlarmUpdated           = onAlarmUpdated;
    if (onAlarmCleared           != null) _onAlarmCleared           = onAlarmCleared;
    if (onAlarmAcknowledgeResult != null) _onAlarmAcknowledgeResult = onAlarmAcknowledgeResult;
    if (onAlarmStatus            != null) _onAlarmStatus            = onAlarmStatus;
    if (onAdminSessionOpened     != null) _onAdminSessionOpened     = onAdminSessionOpened;
    if (onAdminSessionError      != null) _onAdminSessionError      = onAdminSessionError;
    if (onAdminRemoveOk          != null) _onAdminRemoveOk          = onAdminRemoveOk;
    if (onAdminQrResult          != null) _onAdminQrResult          = onAdminQrResult;
    if (onAdminNoQrResult        != null) _onAdminNoQrResult        = onAdminNoQrResult;
    if (onAdminStageOk           != null) _onAdminStageOk           = onAdminStageOk;
    if (onAdminUnstageOk         != null) _onAdminUnstageOk         = onAdminUnstageOk;
    if (onAdminPlaceResult       != null) _onAdminPlaceResult       = onAdminPlaceResult;
    if (onAdminMissingResult     != null) _onAdminMissingResult     = onAdminMissingResult;
    if (onAdminSessionClosed     != null) _onAdminSessionClosed     = onAdminSessionClosed;
    if (onAdminOperationError    != null) _onAdminOperationError    = onAdminOperationError;
  }

  void _registerListeners() {
    if (socket == null) return;

    socket!.on("scan_status",              (d) => _onScanStatus?.call(d));
    socket!.on("deposit_waiting_for_qr",   (d) => _onDepositWaiting?.call(d));
    socket!.on("deposit_result",           (d) => _onDepositResult?.call(d));
    socket!.on("withdraw_waiting_for_action", (d) => _onWithdrawWaiting?.call(d));
    socket!.on("withdraw_result",          (d) => _onWithdrawResult?.call(d));
    socket!.on("verify_waiting_for_action",(d) => _onVerifyWaiting?.call(d));
    socket!.on("verify_result",            (d) => _onVerifyResult?.call(d));
    socket!.on("operation_error",          (d) => _onOperationError?.call(d));
    socket!.on("operation_cancelled",      (d) => _onOperationCancelled?.call(d));

    socket!.on("tracking_started",         (d) => _onTrackingStarted?.call(d));
    socket!.on("tracking_update",          (d) => _onTrackingUpdate?.call(d));
    socket!.on("tracking_failed",          (d) => _onTrackingFailed?.call(d));

    socket!.on("alarm_triggered",          (d) => _onAlarmTriggered?.call(d));
    socket!.on("alarm_updated",            (d) => _onAlarmUpdated?.call(d));
    socket!.on("alarm_cleared",            (d) => _onAlarmCleared?.call(d));
    socket!.on("alarm_acknowledge_result", (d) => _onAlarmAcknowledgeResult?.call(d));
    socket!.on("alarm_status",             (d) => _onAlarmStatus?.call(d));

    socket!.on("admin_session_opened",     (d) => _onAdminSessionOpened?.call(d));
    socket!.on("admin_session_error",      (d) => _onAdminSessionError?.call(d));
    socket!.on("admin_remove_ok",          (d) => _onAdminRemoveOk?.call(d));
    socket!.on("admin_qr_result",          (d) => _onAdminQrResult?.call(d));
    socket!.on("admin_no_qr_result",       (d) => _onAdminNoQrResult?.call(d));
    socket!.on("admin_stage_ok",           (d) => _onAdminStageOk?.call(d));
    socket!.on("admin_unstage_ok",         (d) => _onAdminUnstageOk?.call(d));
    socket!.on("admin_place_result",       (d) => _onAdminPlaceResult?.call(d));
    socket!.on("admin_missing_result",     (d) => _onAdminMissingResult?.call(d));
    socket!.on("admin_session_closed",     (d) => _onAdminSessionClosed?.call(d));
    socket!.on("admin_operation_error",    (d) => _onAdminOperationError?.call(d));
  }

  // ── Scan ──────────────────────────────────────────────

  void toggleScan()    => _emit("toggle_scan", {"toggle": true});
  void requestStatus() => _emit("get_status", {});

  // ── DVW ───────────────────────────────────────────────

  void deposit(String pid)  => _emit("deposit",   {"pid": pid});
  void withdraw(String pid) => _emit("withdraw",  {"pid": pid});
  void qrScanned()          => _emit("qr_scanned", {});

  /// Cancel any active DVW operation for this client.
  /// Works in all stages: waiting, scanning, tracking.
  void cancelOperation() => _emit("cancel_operation", {});

  void verify({
    required String pid,
    required int originalLid,
  }) => _emit("verify", {"pid": pid, "original_lid": originalLid});

  // ── Alarm ─────────────────────────────────────────────

  void acknowledgeAlarm(String password) =>
      _emit("alarm_acknowledge", {"password": password});

  void unsilenceAlarm()    => _emit("alarm_unsilence", {});
  void requestAlarmStatus() => _emit("get_alarm_status", {});

  // ── Admin resolution session ──────────────────────────

  void adminSessionStart(String password) =>
      _emit("admin_session_start", {"password": password});

  void adminRemovePhone(int fromLid) =>
      _emit("admin_remove_phone", {"from_lid": fromLid});

  void adminQrScanned()  => _emit("admin_qr_scanned", {});
  void adminNoQrFound()  => _emit("admin_no_qr_found", {});
  void adminStagePhone() => _emit("admin_stage_phone", {});

  void adminUnstagePhone(String pid) =>
      _emit("admin_unstage_phone", {"pid": pid});

  void adminPlacePhone(int toLid) =>
      _emit("admin_place_phone", {"to_lid": toLid});

  void adminDeclareMissing(String pid) =>
      _emit("admin_declare_missing", {"pid": pid});

  void adminSessionClose() => _emit("admin_session_close", {});

  // ── Cleanup ───────────────────────────────────────────

  void clearDvwCallbacks() {
    _onDepositWaiting    = null;
    _onDepositResult     = null;
    _onWithdrawWaiting   = null;
    _onWithdrawResult    = null;
    _onVerifyWaiting     = null;
    _onVerifyResult      = null;
    _onOperationError    = null;
    _onOperationCancelled = null;
    _onTrackingStarted   = null;
    _onTrackingUpdate    = null;
    _onTrackingFailed    = null;
  }

  void clearAdminCallbacks() {
    _onAdminSessionOpened  = null;
    _onAdminSessionError   = null;
    _onAdminRemoveOk       = null;
    _onAdminQrResult       = null;
    _onAdminNoQrResult     = null;
    _onAdminStageOk        = null;
    _onAdminUnstageOk      = null;
    _onAdminPlaceResult    = null;
    _onAdminMissingResult  = null;
    _onAdminSessionClosed  = null;
    _onAdminOperationError = null;
  }

  void disconnect() {
    socket?.disconnect();
    socket?.dispose();
    socket        = null;
    isConnected   = false;
    _isConnecting = false;
    _onScanStatus = null;
    clearDvwCallbacks();
    clearAdminCallbacks();
    _onAlarmTriggered         = null;
    _onAlarmUpdated           = null;
    _onAlarmCleared           = null;
    _onAlarmAcknowledgeResult = null;
    _onAlarmStatus            = null;
  }

  // ── Internal ──────────────────────────────────────────

  void _emit(String event, Map<String, dynamic> data) {
    if (!isConnected || socket == null) return;
    socket!.emit(event, data);
  }
}