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

  final socketService = SocketService();

  // Opt #28: typed stream subscriptions — cancelled in dispose()
  final List<StreamSubscription> _subs = [];

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

  void _updateScanStatus(Map<String, dynamic> data) {
    if (viewDisposed || !mounted) return;
    if (manualOverride) return;

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
        Navigator.push(
          context,
          MaterialPageRoute(
            builder: (_) => ScanSuccessPage(sid: user, studentName: currentName),
          ),
        ).then((_) => _navigating = false);
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
    Navigator.push(context,
        MaterialPageRoute(builder: (_) => const AdminMenuPage())); // ← fixed #28 + naming
  }

  @override
  void dispose() {
    viewDisposed = true;
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
        child: Column(
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
      ),
    );
  }
}