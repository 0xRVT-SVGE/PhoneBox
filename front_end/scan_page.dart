import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'socket_service.dart';
import 'admin_menu.dart';
import 'auth.dart';
import 'api_service.dart';
import 'offline_op_queue.dart';
import 'scan_success_page.dart';
import 'alarm_page.dart';

import 'webrtc_config.dart';

class ScanPage extends StatefulWidget {
  const ScanPage({super.key});

  @override
  State<ScanPage> createState() => _ScanPageState();
}

class _ScanPageState extends State<ScanPage> {
  final RTCVideoRenderer _remoteRenderer = RTCVideoRenderer();
  RTCPeerConnection? _peerConnection;

  String webrtcStatus  = "Connecting...";
  String scanStatus    = "Idle";

  bool scanning         = false;
  bool manualOverride   = false;
  bool viewDisposed     = false;
  bool _webrtcConnected = false;
  bool _alarmPageOpen   = false;
  bool _isReconnecting  = false;
  // C2: guard against double Navigator.push when two authorized=true events
  // arrive before the first ScanSuccessPage navigation completes.
  bool _navigating      = false;

  // Access-control denial state
  bool   _boxDenied      = false;
  String _boxDeniedMsg   = '';

  final socketService = SocketService();

  // Opt #28: typed stream subscriptions — cancelled in dispose()
  final List<StreamSubscription> _subs = [];

  // ── Top-camera pre-connection for ScanSuccessPage ────────────────────────
  /// How long (seconds) the admin WebRTC connection is kept alive after
  /// leaving ScanSuccessPage before being closed.  Increase if students
  /// typically deposit/retrieve multiple phones in quick succession.
  static const int _kTopCamIdleTimeoutSeconds = 30;

  RTCVideoRenderer?    _preTopRenderer;
  ValueNotifier<bool>? _preTopConnected;
  RTCPeerConnection?   _preTopPc;
  bool _preTopConnecting   = false;
  bool _preTopInitialized  = false;
  Timer? _preTopIdleTimer;

  @override
  void initState() {
    super.initState();
    _initRenderer();
    _connectSocket();
    // F6: open the SQLite DB now so every subsequent enqueue/pendingOps call
    // hits an already-open handle instead of paying openDatabase() overhead.
    OfflineOpQueue.instance.open();
  }

  Future<void> _initRenderer() async {
    await _remoteRenderer.initialize();
    await _startWebRTC();
  }

  // Opt #28: connect() is now parameterless; events come via typed streams
  void _connectSocket() {
    socketService.connect();
    _subs.add(socketService.onScanStatus.listen(_updateScanStatus));
    _subs.add(socketService.onAlarmTriggered.listen(_handleAlarmTriggered));
    _subs.add(socketService.onAlarmCleared.listen(_handleAlarmCleared));
    _subs.add(socketService.onAlarmStatus.listen(_handleAlarmStatus));
    _subs.add(socketService.onBoxAccessDenied.listen(_handleBoxAccessDenied));
  }

  void _handleAlarmStatus(Map<String, dynamic> data) {
    if (!mounted || _alarmPageOpen) return;
    if (data["active"] == true) _handleAlarmTriggered(data);
  }

  void _handleAlarmTriggered(Map<String, dynamic> data) {
    if (!mounted || _alarmPageOpen) return;
    _alarmPageOpen = true;
    Navigator.of(context)
        .push(MaterialPageRoute(
          fullscreenDialog: true,
          builder: (_) => AlarmPage(
            initialMismatches: data["mismatches"] ?? [],
            mainRenderer:      _remoteRenderer,
            isMainConnected:   () => _webrtcConnected,
          ),
        ))
        .then((_) => _alarmPageOpen = false);
  }

  void _handleAlarmCleared(Map<String, dynamic> _) {
    _alarmPageOpen = false;
  }

  // ── Box access denial ────────────────────────────────────────────────────

  void _handleBoxAccessDenied(Map<String, dynamic> data) {
    if (!mounted || viewDisposed) return;
    final reason = data['reason'] as String? ?? 'Access denied for this box.';
    setState(() {
      _boxDenied    = true;
      _boxDeniedMsg = reason;
      scanning      = false;
      scanStatus    = 'Access Denied';
    });
    // Auto-dismiss the denial overlay after 6 seconds
    Future.delayed(const Duration(seconds: 6), () {
      if (mounted) setState(() => _boxDenied = false);
    });
  }

  void _updateScanStatus(Map<String, dynamic> data) {
    if (viewDisposed || !mounted) return;
    if (manualOverride) return;

    // If a denial overlay is showing, hide it on new scan activity
    if (_boxDenied && (data['running'] == true)) {
      setState(() => _boxDenied = false);
    }

    final running = data["running"] ?? false;
    setState(() {
      scanning = running;
      final auth        = data["authorized"]             ?? false;
      final user        = data["user"]                   ?? "";
      final timeout     = data["badge_timeout_exceeded"] ?? false;
      final barcodeOk   = data["barcode_verified"]       ?? false;
      final currentName = data["current_name"]           ?? "Idle";

      if (auth && !_navigating) {
        // C2: set guard before pushing — cleared when ScanSuccessPage pops
        _navigating = true;
        // Pre-connect top camera immediately — connection is already in
        // flight (or done) before the user can tap Take / Put.
        _ensureTopCamConnected(); // intentionally unawaited
        Navigator.push(
          context,
          MaterialPageRoute(
            builder: (_) => ScanSuccessPage(
              sid:                  user,
              studentName:          currentName,
              sharedTopRenderer:    _preTopRenderer,
              topConnectedNotifier: _preTopConnected,
            ),
          ),
        ).then((_) {
          _navigating = false;
          // Start idle timer — closes admin connection after inactivity window
          // so the server is not holding a redundant stream.
          _scheduleTopCamIdle();
          // Flush the WebRTC jitter buffer that accumulated while ScanSuccessPage
          // was on top. A fresh connection gives zero-lag video immediately.
          if (mounted && !viewDisposed && !_alarmPageOpen) {
            _flushAndReconnectMain();
          }
        });
        scanning   = false;
        scanStatus = "Idle";
      } else if (timeout) {
        scanStatus =
            "Timeout. Unable to Verify Badge.\n"
            "Ask for admin's help if it happened more than 2 times";
      } else if (barcodeOk) {
        scanStatus = "Verifying Face Match: $currentName";
      } else if (scanning) {
        scanStatus = "Scanning...";
      } else {
        scanStatus = "Idle";
      }
    });
  }

  Future<void> _startWebRTC() async {
    if (viewDisposed) return;
    try {
      await ApiService.cancelMain();
      _peerConnection = await createPeerConnection(WebRTCConfig.iceConfig);

      _peerConnection!.onTrack = (event) {
        if (viewDisposed || !mounted) return;
        if (event.streams.isNotEmpty) {
          _remoteRenderer.srcObject = event.streams[0];
          if (mounted) setState(() => _webrtcConnected = true);
        }
      };

      _peerConnection!.onConnectionState = (state) async {
        if (viewDisposed || _isReconnecting) return;
        // Only reconnect on 'failed' — 'disconnected' is transient/recoverable.
        if (state == RTCPeerConnectionState.RTCPeerConnectionStateFailed) {
          // AlarmPage is borrowing the renderer — don't restart underneath it
          if (_alarmPageOpen) {
            if (mounted) setState(() => _webrtcConnected = false);
            return;
          }
          _isReconnecting = true;
          if (mounted) {
            setState(() {
              webrtcStatus     = "Reconnecting...";
              _webrtcConnected = false;
            });
          }
          _peerConnection?.onTrack           = null;
          _peerConnection?.onConnectionState = null;
          await _peerConnection?.close();
          _peerConnection = null;
          await Future.delayed(const Duration(seconds: 1));
          if (!viewDisposed && !_alarmPageOpen) {
            _remoteRenderer.srcObject = null;
            await _startWebRTC();
          }
          _isReconnecting = false;
        }
      };

      final offer = await _peerConnection!.createOffer(WebRTCConfig.videoOfferConstraints);
      await _peerConnection!.setLocalDescription(offer);

      final answerSDP = await ApiService.sendOffer(offer.sdp!);
      if (answerSDP != null) {
        await _peerConnection!
            .setRemoteDescription(RTCSessionDescription(answerSDP, 'answer'));
        if (mounted) setState(() => webrtcStatus = "WebRTC Connected");
      } else {
        if (mounted) setState(() => webrtcStatus = "WebRTC Error");
      }
    } catch (_) {
      if (mounted) setState(() => webrtcStatus = "WebRTC Error");
    }
  }

  /// Flush the accumulated WebRTC jitter buffer by tearing down the current
  /// peer connection and opening a fresh one.  Called every time ScanPage
  /// becomes the top route again (returning from admin menu / scan-success
  /// page), because Flutter's native WebRTC jitter buffer never resets itself
  /// and grows with every second spent on another page.
  ///
  /// The old [_remoteRenderer] stream is left intact until [_startWebRTC]
  /// delivers a new one via [onTrack], so the user never sees a black frame.
  Future<void> _flushAndReconnectMain() async {
    if (_isReconnecting || viewDisposed) return;
    _isReconnecting = true;
    try {
      _peerConnection?.onTrack           = null;
      _peerConnection?.onConnectionState = null;
      await _peerConnection?.close();
      _peerConnection = null;
      if (!viewDisposed) await _startWebRTC();
    } finally {
      _isReconnecting = false;
    }
  }

  // ── Top-camera pre-connection management ───────────────────────────────

  /// Start (or reuse) the admin WebRTC connection for the top camera.
  /// Called the moment auth succeeds so the handshake is already complete
  /// by the time the user taps Take / Put inside ScanSuccessPage.
  Future<void> _ensureTopCamConnected() async {
    _preTopIdleTimer?.cancel();
    _preTopIdleTimer = null;
    if (_preTopConnecting) return;
    if (_preTopConnected?.value == true) return;
    _preTopConnecting = true;
    try {
      if (!_preTopInitialized) {
        _preTopRenderer     = RTCVideoRenderer();
        await _preTopRenderer!.initialize();
        _preTopConnected    = ValueNotifier<bool>(false);
        _preTopInitialized  = true;
      }
      await ApiService.cancelAdmin();
      _preTopPc?.onTrack           = null;
      _preTopPc?.onConnectionState = null;
      await _preTopPc?.close();
      _preTopPc                    = null;
      _preTopRenderer!.srcObject   = null;
      _preTopConnected!.value      = false;

      _preTopPc = await createPeerConnection(WebRTCConfig.iceConfig);
      _preTopPc!.onTrack = (event) {
        if (viewDisposed) return;
        if (event.streams.isNotEmpty) {
          _preTopRenderer!.srcObject = event.streams[0];
          _preTopConnected!.value    = true;
        }
      };
      _preTopPc!.onConnectionState = (state) async {
        if (viewDisposed) return;
        if (state == RTCPeerConnectionState.RTCPeerConnectionStateFailed) {
          _preTopConnected?.value = false;
          _preTopPc?.onTrack           = null;
          _preTopPc?.onConnectionState = null;
          await _preTopPc?.close();
          _preTopPc = null;
          if (!viewDisposed) _preTopRenderer?.srcObject = null;
          // Auto-retry once after a brief pause.
          _preTopConnecting = false;
          await Future.delayed(const Duration(seconds: 1));
          if (!viewDisposed) _ensureTopCamConnected();
          return;
        }
      };
      final offer = await _preTopPc!.createOffer(WebRTCConfig.videoOfferConstraints);
      await _preTopPc!.setLocalDescription(offer);
      final sdp = await ApiService.sendOffer(
        offer.sdp!, mode: 'admin', maxRetries: 2,
      );
      if (sdp != null && !viewDisposed) {
        await _preTopPc!
            .setRemoteDescription(RTCSessionDescription(sdp, 'answer'));
      }
    } catch (_) {
      _preTopConnected?.value = false;
    } finally {
      _preTopConnecting = false;
    }
  }

  /// Start the idle countdown after returning from ScanSuccessPage.
  /// If the user re-scans before the timer fires, [_ensureTopCamConnected]
  /// cancels it and reuses the existing connection.
  void _scheduleTopCamIdle() {
    _preTopIdleTimer?.cancel();
    _preTopIdleTimer = Timer(
      const Duration(seconds: _kTopCamIdleTimeoutSeconds),
      _closeTopCam,
    );
  }

  /// Close the top-camera connection and release the server's admin slot.
  Future<void> _closeTopCam() async {
    _preTopIdleTimer?.cancel();
    _preTopIdleTimer = null;
    _preTopPc?.onTrack           = null;
    _preTopPc?.onConnectionState = null;
    await _preTopPc?.close();
    _preTopPc                  = null;
    _preTopRenderer?.srcObject = null;
    _preTopConnected?.value    = false;
    await ApiService.cancelAdmin();
  }

  void toggleScan() {
    if (scanning) {
      setState(() {
        manualOverride = true;
        scanning       = false;
        scanStatus     = "Idle";
      });
      socketService.toggleScan();
    } else {
      setState(() => manualOverride = false);
      socketService.toggleScan();
    }
  }

  void _openAdminMenu() async {
    final auth = AuthService();
    if (!auth.isAdmin) {
      final controller  = TextEditingController();
      bool loginSuccess = false;

      await showDialog(
        context:            context,
        barrierDismissible: false,
        builder: (ctx) => StatefulBuilder(
          builder: (context, setDialogState) => AlertDialog(
            title: const Text("Admin Login"),
            content: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(
                  controller:  controller,
                  autofocus:   true,
                  obscureText: true,
                  decoration:  const InputDecoration(labelText: "Password"),
                  onSubmitted: (value) {
                    if (auth.login(value)) {
                      loginSuccess = true;
                      Navigator.pop(ctx);
                    } else {
                      setDialogState(() {});
                    }
                  },
                ),
                if (!loginSuccess && controller.text.isNotEmpty)
                  const Padding(
                    padding: EdgeInsets.only(top: 8.0),
                    child: Text("Wrong password, try again",
                        style: TextStyle(color: Colors.red)),
                  ),
              ],
            ),
            actions: [
              TextButton(
                onPressed: () {
                  if (auth.login(controller.text)) {
                    loginSuccess = true;
                    Navigator.pop(ctx);
                  } else {
                    setDialogState(() {});
                  }
                },
                child: const Text("Login"),
              ),
              TextButton(
                onPressed: () => Navigator.pop(ctx),
                child: const Text("Cancel"),
              ),
            ],
          ),
        ),
      );
      if (!loginSuccess) return;
    }
    if (!mounted) return;
    Navigator.push(
      context,
      MaterialPageRoute(builder: (_) => const AdminMenuPage()),
    ).then((_) {
      // Flush stale jitter buffer accumulated while AdminMenuPage was on top.
      if (mounted && !viewDisposed && !_alarmPageOpen) {
        _flushAndReconnectMain();
      }
    });
  }

  @override
  void dispose() {
    viewDisposed = true;
    // Cancel idle timer and tear down pre-connected top camera.
    _preTopIdleTimer?.cancel();
    _preTopPc?.onTrack           = null;
    _preTopPc?.onConnectionState = null;
    _preTopPc?.close();
    _preTopPc = null;
    _preTopRenderer?.srcObject = null;
    _preTopRenderer?.dispose();
    _preTopConnected?.dispose();
    // Opt #28: cancel all stream subscriptions — no leaks
    for (final s in _subs) s.cancel();
    _peerConnection?.onTrack           = null;
    _peerConnection?.onConnectionState = null;
    _peerConnection?.close();
    _peerConnection = null;
    _remoteRenderer.srcObject = null;
    _remoteRenderer.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text("Face + Barcode Scanner"),
        actions: [
          IconButton(
            icon:      const Icon(Icons.admin_panel_settings),
            onPressed: _openAdminMenu,
          )
        ],
      ),
      body: SafeArea(
        child: Stack(
          children: [
            // ── Main scan content ─────────────────────────────────────────
            Column(
              children: [
                Expanded(
                  child: Center(
                    child: AspectRatio(
                      aspectRatio: 16 / 9,
                      child: _remoteRenderer.srcObject != null
                          ? RepaintBoundary(child: RTCVideoView(_remoteRenderer))
                          : Container(color: Colors.black),
                    ),
                  ),
                ),
                const SizedBox(height: 12),
                Text(webrtcStatus,
                    style: const TextStyle(fontSize: 16, color: Colors.grey)),
                const SizedBox(height: 4),
                Text(scanStatus,
                    textAlign: TextAlign.center,
                    style: const TextStyle(fontSize: 18)),
                const SizedBox(height: 10),
                Padding(
                  padding: const EdgeInsets.only(bottom: 16),
                  child: ElevatedButton.icon(
                    icon:  Icon(scanning ? Icons.stop : Icons.play_arrow),
                    label: Text(scanning ? "Stop Scan" : "Start Scan"),
                    style: ElevatedButton.styleFrom(
                      backgroundColor: scanning ? Colors.red : Colors.green,
                      minimumSize:     const Size(160, 45),
                    ),
                    onPressed: toggleScan,
                  ),
                ),
              ],
            ),

            // ── Box access denial overlay ─────────────────────────────────
            // Shown when server rejects a badge scan due to group mismatch.
            // Tapping anywhere or waiting 6 s dismisses it.
            if (_boxDenied)
              Positioned.fill(
                child: GestureDetector(
                  onTap: () => setState(() => _boxDenied = false),
                  child: Container(
                    color: Colors.black87,
                    padding: const EdgeInsets.symmetric(
                        horizontal: 32, vertical: 48),
                    child: Column(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        const Icon(Icons.lock_outline,
                            size: 72, color: Colors.redAccent),
                        const SizedBox(height: 24),
                        const Text(
                          'Access Denied',
                          style: TextStyle(
                            color:      Colors.white,
                            fontSize:   28,
                            fontWeight: FontWeight.bold,
                          ),
                        ),
                        const SizedBox(height: 16),
                        Text(
                          _boxDeniedMsg,
                          textAlign: TextAlign.center,
                          style: const TextStyle(
                            color:    Colors.white70,
                            fontSize: 18,
                          ),
                        ),
                        const SizedBox(height: 32),
                        TextButton(
                          onPressed: () => setState(() => _boxDenied = false),
                          child: const Text(
                            'Tap to dismiss',
                            style: TextStyle(
                                color: Colors.white38, fontSize: 14),
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}