import 'package:socket_io_client/socket_io_client.dart' as IO;

class SocketService {
  static final SocketService _instance = SocketService._internal();
  factory SocketService() => _instance;
  SocketService._internal();

  IO.Socket? socket;
  bool isConnected = false;
  bool _isConnecting = false;

  // Callback holders for operation events
  Function(dynamic)? _onScanStatus;
  Function(dynamic)? _onDepositWaiting;
  Function(dynamic)? _onDepositResult;
  Function(dynamic)? _onWithdrawWaiting;
  Function(dynamic)? _onWithdrawResult;
  Function(dynamic)? _onVerifyWaiting;
  Function(dynamic)? _onVerifyResult;
  Function(dynamic)? _onOperationError;

  // Alarm callbacks
  Function(dynamic)? _onAlarmTriggered;
  Function(dynamic)? _onAlarmUpdated;
  Function(dynamic)? _onAlarmCleared;
  Function(dynamic)? _onAlarmAcknowledgeResult;
  Function(dynamic)? _onAlarmStatus;

  void connect({
    Function(dynamic)? onScanStatus,
    Function(dynamic)? onDepositWaiting,
    Function(dynamic)? onDepositResult,
    Function(dynamic)? onWithdrawWaiting,
    Function(dynamic)? onWithdrawResult,
    Function(dynamic)? onVerifyWaiting,
    Function(dynamic)? onVerifyResult,
    Function(dynamic)? onOperationError,
    Function(dynamic)? onAlarmTriggered,
    Function(dynamic)? onAlarmUpdated,
    Function(dynamic)? onAlarmCleared,
    Function(dynamic)? onAlarmAcknowledgeResult,
    Function(dynamic)? onAlarmStatus,
  }) {
    // CRITICAL: Only connect once
    if (isConnected || _isConnecting) {
      print("Socket already connected or connecting");
      // Update callbacks even if already connected
      _updateCallbacks(
        onScanStatus: onScanStatus,
        onDepositWaiting: onDepositWaiting,
        onDepositResult: onDepositResult,
        onWithdrawWaiting: onWithdrawWaiting,
        onWithdrawResult: onWithdrawResult,
        onVerifyWaiting: onVerifyWaiting,
        onVerifyResult: onVerifyResult,
        onOperationError: onOperationError,
        onAlarmTriggered: onAlarmTriggered,
        onAlarmUpdated: onAlarmUpdated,
        onAlarmCleared: onAlarmCleared,
        onAlarmAcknowledgeResult: onAlarmAcknowledgeResult,
        onAlarmStatus: onAlarmStatus,
      );
      return;
    }

    _isConnecting = true;

    socket = IO.io(
      "http://localhost:5000",
      <String, dynamic>{
        "transports": ["websocket"],
        "autoConnect": true,
      },
    );

    socket!.onConnect((_) {
      isConnected = true;
      _isConnecting = false;
      print("Connected to backend socket");
      requestStatus();
      requestAlarmStatus(); // re-sync alarm state on every (re)connect
    });

    socket!.onDisconnect((_) {
      isConnected = false;
      _isConnecting = false;
      print("Socket disconnected");
    });

    // Store callbacks
    _updateCallbacks(
      onScanStatus: onScanStatus,
      onDepositWaiting: onDepositWaiting,
      onDepositResult: onDepositResult,
      onWithdrawWaiting: onWithdrawWaiting,
      onWithdrawResult: onWithdrawResult,
      onVerifyWaiting: onVerifyWaiting,
      onVerifyResult: onVerifyResult,
      onOperationError: onOperationError,
      onAlarmTriggered: onAlarmTriggered,
      onAlarmUpdated: onAlarmUpdated,
      onAlarmCleared: onAlarmCleared,
      onAlarmAcknowledgeResult: onAlarmAcknowledgeResult,
      onAlarmStatus: onAlarmStatus,
    );

    // Register listeners ONCE
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
    Function(dynamic)? onAlarmTriggered,
    Function(dynamic)? onAlarmUpdated,
    Function(dynamic)? onAlarmCleared,
    Function(dynamic)? onAlarmAcknowledgeResult,
    Function(dynamic)? onAlarmStatus,
  }) {
    if (onScanStatus != null) _onScanStatus = onScanStatus;
    if (onDepositWaiting != null) _onDepositWaiting = onDepositWaiting;
    if (onDepositResult != null) _onDepositResult = onDepositResult;
    if (onWithdrawWaiting != null) _onWithdrawWaiting = onWithdrawWaiting;
    if (onWithdrawResult != null) _onWithdrawResult = onWithdrawResult;
    if (onVerifyWaiting != null) _onVerifyWaiting = onVerifyWaiting;
    if (onVerifyResult != null) _onVerifyResult = onVerifyResult;
    if (onOperationError != null) _onOperationError = onOperationError;
    if (onAlarmTriggered != null) _onAlarmTriggered = onAlarmTriggered;
    if (onAlarmUpdated != null) _onAlarmUpdated = onAlarmUpdated;
    if (onAlarmCleared != null) _onAlarmCleared = onAlarmCleared;
    if (onAlarmAcknowledgeResult != null) _onAlarmAcknowledgeResult = onAlarmAcknowledgeResult;
    if (onAlarmStatus != null) _onAlarmStatus = onAlarmStatus;
  }

  void _registerListeners() {
    if (socket == null) return;

    // ==================== SCAN STATUS LISTENER ====================
    socket!.on("scan_status", (data) {
      _onScanStatus?.call(data);
    });

    // ==================== DEPOSIT LISTENERS ====================
    socket!.on("deposit_waiting_for_qr", (data) {
      print("Flutter received: deposit_waiting_for_qr");
      print("Data: $data");
      _onDepositWaiting?.call(data);
    });

    socket!.on("deposit_result", (data) {
      print("Flutter received: deposit_result");
      print("Data: $data");
      _onDepositResult?.call(data);
    });

    // ==================== WITHDRAW LISTENERS ====================
    socket!.on("withdraw_waiting_for_action", (data) {
      print("Flutter received: withdraw_waiting_for_action");
      print("Data: $data");
      _onWithdrawWaiting?.call(data);
    });

    socket!.on("withdraw_result", (data) {
      print("Flutter received: withdraw_result");
      print("Data: $data");
      _onWithdrawResult?.call(data);
    });

    // ==================== VERIFY LISTENERS ====================
    socket!.on("verify_waiting_for_action", (data) {
      print("Flutter received: verify_waiting_for_action");
      print("Data: $data");
      _onVerifyWaiting?.call(data);
    });

    socket!.on("verify_result", (data) {
      print("Flutter received: verify_result");
      print("Data: $data");
      _onVerifyResult?.call(data);
    });

    // ==================== ERROR LISTENER ====================
    socket!.on("operation_error", (data) {
      print("Flutter received: operation_error");
      print("Data: $data");
      _onOperationError?.call(data);
    });

    // ==================== ALARM LISTENERS ====================
    socket!.on("alarm_triggered", (data) => _onAlarmTriggered?.call(data));
    socket!.on("alarm_updated", (data) => _onAlarmUpdated?.call(data));
    socket!.on("alarm_cleared", (data) => _onAlarmCleared?.call(data));
    socket!.on("alarm_acknowledge_result", (data) => _onAlarmAcknowledgeResult?.call(data));
    socket!.on("alarm_status", (data) => _onAlarmStatus?.call(data));

    print("Socket listeners registered");
  }

  // ==================== SCAN OPERATIONS ====================
  void listenScanStatus(Function(dynamic) onScanStatus) {
    // No longer needed - scan_status is already registered
    print("listenScanStatus is deprecated, use connect() callback instead");
  }

  void toggleScan() {
    if (isConnected && socket != null) {
      socket!.emit("toggle_scan", {"toggle": true});
      print("[>] toggle_scan emitted");
    } else {
      print("Socket not connected!");
    }
  }

  void requestStatus() {
    if (isConnected && socket != null) {
      socket!.emit("get_status", {});
      print("[>] get_status emitted");
    }
  }

  // ==================== DEPOSIT OPERATION ====================
  void deposit(String pid) {
    if (!isConnected || socket == null) {
      print("Socket not connected!");
      return;
    }
    print("Emitting deposit for PID: $pid");
    socket!.emit("deposit", {"pid": pid});
  }

  // ==================== WITHDRAW OPERATION ====================
  void withdraw(String pid) {
    if (!isConnected || socket == null) {
      print("Socket not connected!");
      return;
    }
    print("Emitting withdraw for PID: $pid");
    socket!.emit("withdraw", {"pid": pid});
  }

  // ==================== VERIFY OPERATION ====================
  void verify({
    required String pid,
    required int originalLid,
    required int targetLid,
  }) {
    if (!isConnected || socket == null) {
      print("Socket not connected!");
      return;
    }
    print("Emitting verify: PID=$pid, $originalLid → $targetLid");
    socket!.emit("verify", {
      "pid": pid,
      "original_lid": originalLid,
      "target_lid": targetLid,
    });
  }

  // ==================== QR SCANNED ====================
  void qrScanned() {
    if (!isConnected || socket == null) {
      print("Socket not connected!");
      return;
    }
    print("Emitting QR scanned");
    socket!.emit("qr_scanned", {});
  }

  // ==================== ALARM OPERATIONS ====================
  void acknowledgeAlarm(String password) {
    if (!isConnected || socket == null) return;
    socket!.emit("alarm_acknowledge", {"password": password});
  }

  void requestAlarmStatus() {
    if (!isConnected || socket == null) return;
    socket!.emit("get_alarm_status", {});
  }

  // ==================== CLEANUP ====================
  void clearOperationCallbacks() {
    _onDepositWaiting = null;
    _onDepositResult = null;
    _onWithdrawWaiting = null;
    _onWithdrawResult = null;
    _onVerifyWaiting = null;
    _onVerifyResult = null;
    _onOperationError = null;
    _onAlarmTriggered = null;
    _onAlarmUpdated = null;
    _onAlarmCleared = null;
    _onAlarmAcknowledgeResult = null;
    _onAlarmStatus = null;
    // Keep scan status callback if needed
  }

  void disconnect() {
    print("🔌 Disconnecting socket...");
    socket?.disconnect();
    socket?.dispose();
    socket = null;
    isConnected = false;
    _isConnecting = false;
    clearOperationCallbacks();
    _onScanStatus = null;
  }
}