import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'socket_service.dart';
import 'api_service.dart';
import 'webrtc_config.dart';
import 'offline_op_queue.dart'; // Opt #33

import 'shimmer_widgets.dart'; // Opt #31


// ── Shared location label helper ─────────────────────────
String phoneLocationLabel(Map<String, dynamic> p) {
  final lid = p['lid'];
  final x   = p['x'];
  final y   = p['y'];
  if (lid == null && x == null) return 'N/A';
  final slotNum = lid != null ? (lid as num).toInt() + 1 : null;
  if (slotNum != null && x != null && y != null) return 'slot $slotNum (row $x, col $y)';
  if (slotNum != null) return 'slot $slotNum';
  if (x != null && y != null) return 'row $x, col $y';
  return 'N/A';
}

// ══════════════════════════════════════════════════════════
// SCAN SUCCESS PAGE
// ══════════════════════════════════════════════════════════

class ScanSuccessPage extends StatefulWidget {
  final String sid;
  final String studentName;

  const ScanSuccessPage({
    super.key,
    required this.sid,
    required this.studentName,
  });

  @override
  State<ScanSuccessPage> createState() => _ScanSuccessPageState();
}

class _ScanSuccessPageState extends State<ScanSuccessPage> {
  final _socketService = SocketService();
  bool          _loading    = true;
  List<dynamic> _phones     = [];
  int           _pendingOps = 0;   // Opt #33: queued op count

  // ── Top-camera pre-connection (Opt #27) ──────────────────────────────────
  final _topRenderer    = RTCVideoRenderer();
  RTCPeerConnection?    _topPc;
  final _topConnected   = ValueNotifier<bool>(false);
  bool _topConnecting   = false;
  bool _topPageDisposed = false;

  @override
  void initState() {
    super.initState();
    _loadPhones();
    _topRenderer.initialize().then((_) {
      if (!_topPageDisposed) _preconnectTopCamera();
    });
  }


  @override
  void dispose() {
    _topPageDisposed = true;
    _topPc?.onTrack            = null;
    _topPc?.onConnectionState  = null;
    _topPc?.close();
    _topPc = null;
    _topRenderer.srcObject = null;
    _topRenderer.dispose();
    _topConnected.dispose();
    // DVWBottomSheet manages its own subscriptions — nothing to cancel here
    super.dispose();
  }

  Future<void> _preconnectTopCamera() async {
    if (_topConnecting || _topConnected.value || _topPageDisposed) return;
    _topConnecting = true;
    try {
      await ApiService.cancelAdmin();
      _topPc = await createPeerConnection(WebRTCConfig.iceConfig);
      _topPc!.onTrack = (event) {
        if (_topPageDisposed || !mounted) return;
        if (event.streams.isNotEmpty) {
          _topRenderer.srcObject = event.streams[0];
          _topConnected.value    = true;
        }
      };
      _topPc!.onConnectionState = (state) async {
        if (_topPageDisposed) return;
        if (state == RTCPeerConnectionState.RTCPeerConnectionStateDisconnected ||
            state == RTCPeerConnectionState.RTCPeerConnectionStateFailed) {
          _topConnected.value = false;
          await _topPc?.close();
          _topPc = null;
          await Future.delayed(const Duration(seconds: 3));
          if (!_topPageDisposed) {
            _topRenderer.srcObject = null;
            _topConnecting = false;
            _preconnectTopCamera();
          }
        }
      };
      final offer = await _topPc!.createOffer(WebRTCConfig.videoOfferConstraints);
      await _topPc!.setLocalDescription(offer);
      final sdp = await ApiService.sendOffer(
        offer.sdp!, mode: 'admin', maxRetries: 2,
      );
      if (sdp != null && !_topPageDisposed) {
        await _topPc!.setRemoteDescription(RTCSessionDescription(sdp, 'answer'));
      }
    } catch (_) {
      // Silently ignore; DVWBottomSheet falls back to its own connection.
    } finally {
      _topConnecting = false;
    }
  }

  Future<void> _loadPhones() async {
    final data = await ApiService.getPhones(widget.sid);
    if (!mounted) return;
    setState(() {
      _phones  = data ?? [];
      _loading = false;
    });
    // Opt #33: drain any queued ops now that we have a fresh list
    _drainAndRefresh();
  }

  /// Opt #33 — replay pending ops then refresh the phone list once more.
  Future<void> _drainAndRefresh() async {
    final queue = OfflineOpQueue.instance;
    final count = await queue.pendingCount();
    if (mounted) setState(() => _pendingOps = count);

    if (count > 0) {
      final replayed = await queue.drainAndReplay(_socketService);
      if (replayed > 0 && mounted) {
        setState(() => _pendingOps = 0);
        // Brief delay to let the server process the replayed op
        await Future.delayed(const Duration(milliseconds: 600));
        final fresh = await ApiService.getPhones(widget.sid);
        if (mounted) setState(() => _phones = fresh ?? _phones);
      }
    }
  }


  void _startOperation(String pid, bool isDeposit) {
    if (isDeposit) {
      _socketService.deposit(pid);
    } else {
      _socketService.withdraw(pid);
    }
    showModalBottomSheet(
      context:             context,
      isDismissible:       false,
      enableDrag:          false,
      isScrollControlled:  true,
      backgroundColor:     Colors.transparent,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (_) => DVWBottomSheet(
        pid:                  pid,
        isDeposit:            isDeposit,
        socketService:        _socketService,
        onComplete:           _loadPhones,
        sharedTopRenderer:    _topRenderer,
        topConnectedNotifier: _topConnected,
      ),
    );
  }

  Widget _buildPhoneCard(Map<String, dynamic> p) {
    final pid      = p['pid'].toString();
    final model    = p['model'] as String? ?? 'Unknown Model';
    final isStored = p['is_stored'] == true;
    final location = phoneLocationLabel(p);

    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 10, horizontal: 14),
        child: Row(children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(model,
                    style: const TextStyle(
                        fontSize: 16, fontWeight: FontWeight.bold)),
                const SizedBox(height: 4),
                Text('PID: $pid',
                    style: const TextStyle(fontSize: 12, color: Colors.grey)),
                Text('Location: $location'),
                Text(
                  isStored ? '📦 Stored' : '🎒 With you',
                  style: TextStyle(
                      color: isStored ? Colors.green[700] : Colors.blue[700],
                      fontWeight: FontWeight.w600),
                ),
              ],
            ),
          ),
          Column(children: [
            _ActionButton(
              label:     'Take',
              color:     Colors.green,
              enabled:   isStored,
              onPressed: () => _startOperation(pid, false),
            ),
            const SizedBox(height: 8),
            _ActionButton(
              label:     'Put',
              color:     Colors.blue,
              enabled:   !isStored,
              onPressed: () => _startOperation(pid, true),
            ),
          ]),
        ]),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title:   Text(widget.studentName),
        leading: const BackButton(),
      ),
      body: Column(
        children: [
          // Opt #33: pending-ops banner
          if (_pendingOps > 0)
            Material(
              color: Colors.orange.shade800,
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
                child: Row(children: [
                  const Icon(Icons.wifi_off, color: Colors.white, size: 16),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      '$_pendingOps pending operation${_pendingOps == 1 ? '' : 's'} '
                      '— will retry when reconnected',
                      style: const TextStyle(
                          color: Colors.white, fontSize: 12),
                    ),
                  ),
                ]),
              ),
            ),
          // Opt #31: skeleton instead of spinner
          Expanded(
            child: _loading
                ? const PhoneListSkeleton()
                : _phones.isEmpty
                    ? const Center(child: Text('No phones found'))
                    : ListView.builder(
                        itemCount:   _phones.length,
                        itemBuilder: (_, i) =>
                            _buildPhoneCard(_phones[i] as Map<String, dynamic>),
                      ),
          ),
        ],
      ),
    );
  }

}

// ── Shared action button ──────────────────────────────────

class _ActionButton extends StatelessWidget {
  final String label;
  final Color color;
  final bool enabled;
  final VoidCallback onPressed;

  const _ActionButton({
    required this.label,
    required this.color,
    required this.enabled,
    required this.onPressed,
  });

  @override
  Widget build(BuildContext context) => ElevatedButton(
        onPressed: enabled ? onPressed : null,
        style: ElevatedButton.styleFrom(
            backgroundColor: color,
            fixedSize: const Size(80, 36)),
        child: Text(label),
      );
}

// ══════════════════════════════════════════════════════════
// DVW BOTTOM SHEET  — Opt #28: full stream migration
// ══════════════════════════════════════════════════════════

enum DvwStep { waiting, autoScanning, tracking, success, error }

class DVWBottomSheet extends StatefulWidget {
  final String pid;
  final bool isDeposit;
  final SocketService socketService;
  final VoidCallback onComplete;

  final RTCVideoRenderer?    sharedTopRenderer;
  final ValueNotifier<bool>? topConnectedNotifier;

  const DVWBottomSheet({
    super.key,
    required this.pid,
    required this.isDeposit,
    required this.socketService,
    required this.onComplete,
    this.sharedTopRenderer,
    this.topConnectedNotifier,
  });

  @override
  State<DVWBottomSheet> createState() => _DVWBottomSheetState();
}

class _DVWBottomSheetState extends State<DVWBottomSheet> {
  DvwStep _step      = DvwStep.waiting;
  int?    _slot;
  String? _errorText;
  bool    _qrVisible = true;

  Timer? _successTimer;
  int    _successCountdown = 2;

  bool _selfCancelled = false;

  late final RTCVideoRenderer _topRenderer;
  bool _ownsTopRenderer = false;

  RTCPeerConnection? _topPc;
  bool _topConnected   = false;
  bool _topConnecting  = false;
  bool _disposed       = false;
  bool _isReconnecting = false;

  // Opt #28: typed stream subscriptions
  final List<StreamSubscription> _subs = [];

  @override
  void initState() {
    super.initState();

    if (widget.sharedTopRenderer != null) {
      _topRenderer     = widget.sharedTopRenderer!;
      _ownsTopRenderer = false;
      _topConnected    = widget.topConnectedNotifier?.value
                         ?? (_topRenderer.srcObject != null);
      widget.topConnectedNotifier?.addListener(_onParentConnectionChanged);
    } else {
      _topRenderer     = RTCVideoRenderer();
      _ownsTopRenderer = true;
      _topRenderer.initialize();
    }

    _registerCallbacks();
  }

  void _onParentConnectionChanged() {
    if (!mounted) return;
    setState(() => _topConnected = widget.topConnectedNotifier!.value);
  }

  @override
  void dispose() {
    _disposed = true;
    _successTimer?.cancel();
    widget.topConnectedNotifier?.removeListener(_onParentConnectionChanged);
    // Opt #28: cancel all stream subscriptions
    for (final s in _subs) s.cancel();
    _disconnectTopCamera();
    if (_ownsTopRenderer) _topRenderer.dispose();
    super.dispose();
  }

  // ── Top camera ────────────────────────────────────────────────────────────

  Future<void> _connectTopCamera() async {
    if (!_ownsTopRenderer) return;
    if (_topConnecting || _topConnected || _disposed) return;
    _topConnecting = true;
    try {
      await ApiService.cancelAdmin();
      _topPc = await createPeerConnection(WebRTCConfig.iceConfig);
      _topPc!.onTrack = (event) {
        if (_disposed || !mounted) return;
        if (event.streams.isNotEmpty) {
          setState(() {
            _topRenderer.srcObject = event.streams[0];
            _topConnected          = true;
          });
        }
      };
      _topPc!.onConnectionState = (state) {
        if (_disposed) return;
        if (state == RTCPeerConnectionState.RTCPeerConnectionStateDisconnected ||
            state == RTCPeerConnectionState.RTCPeerConnectionStateFailed) {
          if (mounted) setState(() => _topConnected = false);
        }
      };
      final offer = await _topPc!.createOffer(WebRTCConfig.videoOfferConstraints);
      await _topPc!.setLocalDescription(offer);
      final sdp = await ApiService.sendOffer(offer.sdp!, mode: 'admin',
          maxRetries: 2);
      if (sdp != null && !_disposed) {
        await _topPc!.setRemoteDescription(RTCSessionDescription(sdp, 'answer'));
      }
    } catch (_) {
      if (mounted) setState(() => _topConnected = false);
    } finally {
      _topConnecting = false;
    }
  }

  void _disconnectTopCamera() {
    _topPc?.onTrack            = null;
    _topPc?.onConnectionState  = null;
    _topPc?.close();
    _topPc = null;
    if (_ownsTopRenderer) {
      _topRenderer.srcObject = null;
      _topConnected  = false;
      _topConnecting = false;
      ApiService.cancelAdmin();
    }
  }

  // ── Success auto-close ────────────────────────────────────────────────────

  void _startSuccessTimer() {
    _successCountdown = 2;
    _successTimer = Timer.periodic(const Duration(seconds: 1), (t) {
      if (!mounted) { t.cancel(); return; }
      if (_successCountdown <= 1) {
        t.cancel();
        if (mounted) Navigator.of(context).pop();
      } else {
        setState(() => _successCountdown--);
      }
    });
  }

  // ── Opt #28: stream subscriptions ────────────────────────────────────────
  void _registerCallbacks() {
    final s = widget.socketService;
    s.connect(); // idempotent

    _subs.add(s.onDepositWaiting.listen((data) {
      if (!mounted) return;
      setState(() {
        _slot = data['slot'] as int? ?? ((data['lid'] as int? ?? 0) + 1);
        _step = DvwStep.autoScanning;
      });
      _connectTopCamera();
    }));

    _subs.add(s.onWithdrawWaiting.listen((data) {
      if (!mounted) return;
      setState(() {
        _slot = data['slot'] as int? ?? ((data['lid'] as int? ?? 0) + 1);
        _step = DvwStep.autoScanning;
      });
      _connectTopCamera();
    }));

    _subs.add(s.onDepositResult.listen(_handleResult));
    _subs.add(s.onWithdrawResult.listen(_handleResult));

    _subs.add(s.onOperationError.listen((data) {
      if (!mounted) return;
      if (_selfCancelled) return;
      setState(() {
        _step      = DvwStep.error;
        _errorText = data['message'] as String? ?? 'Unknown error';
      });
      _disconnectTopCamera();
    }));

    _subs.add(s.onOperationCancelled.listen((_) {
      if (_selfCancelled) return;
      if (mounted) Navigator.of(context).pop();
    }));

    _subs.add(s.onTrackingStarted.listen((_) {
      if (!mounted) return;
      setState(() {
        _step      = DvwStep.tracking;
        _qrVisible = true;
      });
      _connectTopCamera();
    }));

    _subs.add(s.onTrackingUpdate.listen((data) {
      if (!mounted) return;
      final visible = data['qr_visible'] as bool? ?? true;
      if (visible != _qrVisible) setState(() => _qrVisible = visible);
    }));

    _subs.add(s.onTrackingFailed.listen((data) {
      if (!mounted) return;
      if (_selfCancelled) return;
      setState(() {
        _step      = DvwStep.error;
        _errorText = _trackingMsg(data['reason'] as String? ?? '');
      });
      _disconnectTopCamera();
    }));
  }

  void _handleResult(Map<String, dynamic> data) {
    if (!mounted) return;
    _disconnectTopCamera();
    if (data['status'] == 'success') {
      widget.onComplete();
      setState(() {
        _step             = DvwStep.success;
        _successCountdown = 2;
      });
      _startSuccessTimer();
    } else {
      if (_selfCancelled) return;
      setState(() {
        _step      = DvwStep.error;
        _errorText = data['message'] as String? ?? 'Operation failed';
      });
    }
  }

  void _onCancel() {
    _selfCancelled = true;
    widget.socketService.cancelOperation();
    _disconnectTopCamera();
    Navigator.of(context).pop();
  }

  static String _trackingMsg(String reason) {
    const map = {
      'qr_lost':        'QR code disappeared before reaching the slot. Keep it visible.',
      'out_of_frame':   'Phone left the camera view. Move directly toward the slot.',
      'timeout':        'Placement timed out. Please retry.',
      'detect_timeout': 'Phone not detected. Make sure it enters the camera view.',
    };
    return map[reason] ?? 'Placement failed. Please retry.';
  }

  // ── Build ─────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    final screenH    = MediaQuery.of(context).size.height;
    final showCamera = _step == DvwStep.tracking || _step == DvwStep.autoScanning;

    return Container(
      constraints: BoxConstraints(
        maxHeight: showCamera ? screenH * 0.85 : screenH * 0.55,
      ),
      decoration: const BoxDecoration(
        color:        Color(0xFF1C1C1E),
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Padding(
            padding: const EdgeInsets.only(top: 10, bottom: 4),
            child: const SizedBox(
              width: 36, height: 4,
              child: DecoratedBox(
                decoration: BoxDecoration(
                    color:        Colors.white24,
                    borderRadius: BorderRadius.all(Radius.circular(2))),
              ),
            ),
          ),

          if (showCamera)
            Expanded(
              child: Stack(
                fit: StackFit.expand,
                children: [
                  ClipRRect(
                    borderRadius: const BorderRadius.vertical(
                        top: Radius.circular(16)),
                    // Opt #30: RepaintBoundary on video
                    child: _topConnected && _topRenderer.srcObject != null
                        ? RepaintBoundary(
                            child: RTCVideoView(
                              _topRenderer,
                              objectFit: RTCVideoViewObjectFit
                                  .RTCVideoViewObjectFitContain,
                            ),
                          )
                        : Container(
                            color: Colors.black,
                            child: Center(
                              child: Column(
                                  mainAxisSize: MainAxisSize.min,
                                  children: [
                                    const SizedBox(
                                        width: 24, height: 24,
                                        child: CircularProgressIndicator(
                                            strokeWidth: 2,
                                            color: Colors.white38)),
                                    const SizedBox(height: 8),
                                    Text(
                                      _step == DvwStep.autoScanning
                                          ? 'Preparing top camera...'
                                          : 'Connecting top camera...',
                                      style: const TextStyle(
                                          color: Colors.white38, fontSize: 12)),
                                  ]),
                            ),
                          ),
                  ),
                  const Positioned(
                    bottom: 10, left: 12,
                    child: _CamBadge(
                        label: 'TOP CAM',
                        icon:  Icons.videocam_outlined),
                  ),
                  if (_step == DvwStep.tracking)
                    Positioned(
                      bottom: 10, right: 12,
                      child: _QrStatusOverlayBadge(qrVisible: _qrVisible),
                    ),
                ],
              ),
            ),

          Padding(
            padding: EdgeInsets.fromLTRB(
                24, 12, 24,
                MediaQuery.of(context).viewInsets.bottom + 24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: _buildContent(),
            ),
          ),
        ],
      ),
    );
  }

  List<Widget> _buildContent() {
    switch (_step) {
      case DvwStep.waiting:
        return [
          const CircularProgressIndicator(),
          const SizedBox(height: 16),
          Text(
            widget.isDeposit
                ? 'Finding a free slot...'
                : 'Looking up your phone...',
            style: const TextStyle(fontSize: 16, color: Colors.white),
          ),
          const SizedBox(height: 24),
          _cancelBtn(),
        ];

      case DvwStep.autoScanning:
        final slotLabel   = 'slot ${_slot ?? '?'}';
        final instruction = widget.isDeposit
            ? 'Hold the QR code under the top camera,\n'
              'then carry the phone to $slotLabel.'
            : 'Remove your phone from $slotLabel,\n'
              'then hold its QR code under the camera.';
        return [
          const SizedBox(width: 36, height: 36,
              child: CircularProgressIndicator(strokeWidth: 3)),
          const SizedBox(height: 16),
          Icon(
            widget.isDeposit ? Icons.login_outlined : Icons.logout_outlined,
            size:  40,
            color: widget.isDeposit ? Colors.blue : Colors.green,
          ),
          const SizedBox(height: 10),
          Text(instruction,
              textAlign: TextAlign.center,
              style: const TextStyle(
                  fontSize: 16, fontWeight: FontWeight.bold,
                  color: Colors.white)),
          const SizedBox(height: 8),
          Text('Scanning for QR code... (up to 15 s)',
              style: TextStyle(fontSize: 13, color: Colors.grey[500])),
          const SizedBox(height: 20),
          _cancelBtn(),
        ];

      case DvwStep.tracking:
        return [
          Text(
            'Move to slot ${_slot ?? '?'} — keep QR visible until placed',
            textAlign: TextAlign.center,
            style: const TextStyle(
                fontSize: 15, fontWeight: FontWeight.w600, color: Colors.white),
          ),
          const SizedBox(height: 12),
          _cancelBtn(),
        ];

      case DvwStep.success:
        final verb = widget.isDeposit ? 'stored' : 'retrieved';
        return [
          const Icon(Icons.check_circle_outline, size: 52, color: Colors.green),
          const SizedBox(height: 14),
          Text('Phone ${widget.pid} $verb successfully!',
              textAlign: TextAlign.center,
              style: const TextStyle(
                  fontSize: 17, fontWeight: FontWeight.bold, color: Colors.white)),
          const SizedBox(height: 20),
          ElevatedButton(
            style: ElevatedButton.styleFrom(
              backgroundColor: Colors.green,
              minimumSize:     const Size(double.infinity, 48),
              shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12)),
            ),
            onPressed: () {
              _successTimer?.cancel();
              Navigator.of(context).pop();
            },
            child: Text(
              'Continue  ($_successCountdown)',
              style: const TextStyle(
                  fontSize: 15, fontWeight: FontWeight.w600,
                  color: Colors.white),
            ),
          ),
          const SizedBox(height: 6),
          Text(
            'Closing automatically in $_successCountdown '
            'second${_successCountdown == 1 ? '' : 's'}',
            style: const TextStyle(color: Colors.white38, fontSize: 12),
          ),
        ];

      case DvwStep.error:
        return [
          const Icon(Icons.error_outline, size: 48, color: Colors.red),
          const SizedBox(height: 14),
          Text(_errorText ?? 'Something went wrong.',
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 15, color: Colors.white70)),
          const SizedBox(height: 20),
          TextButton(
              onPressed: () => Navigator.of(context).pop(),
              child: const Text('Close')),
        ];
    }
  }

  Widget _cancelBtn() => TextButton(
        onPressed: _onCancel,
        child: const Text('Cancel', style: TextStyle(color: Colors.grey)),
      );
}

// ── Small camera badge ────────────────────────────────────

class _CamBadge extends StatelessWidget {
  final String   label;
  final IconData icon;
  const _CamBadge({required this.label, required this.icon});

  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(
          color:        Colors.black.withOpacity(0.55),
          borderRadius: BorderRadius.circular(6),
          border:       Border.all(color: Colors.white.withOpacity(0.1)),
        ),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          Icon(icon, color: Colors.white38, size: 12),
          const SizedBox(width: 5),
          Text(label,
              style: const TextStyle(
                  color: Colors.white54, fontSize: 11,
                  fontWeight: FontWeight.w500)),
        ]),
      );
}

// ── QR status overlay badge ───────────────────────────────

class _QrStatusOverlayBadge extends StatelessWidget {
  final bool qrVisible;
  const _QrStatusOverlayBadge({required this.qrVisible});

  @override
  Widget build(BuildContext context) {
    final color = qrVisible ? Colors.green : Colors.orange;
    final icon  = qrVisible ? Icons.qr_code_2 : Icons.qr_code_2_outlined;
    final label = qrVisible ? 'QR OK' : 'QR NOT VISIBLE';
    return AnimatedContainer(
      duration: const Duration(milliseconds: 300),
      padding:  const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
      decoration: BoxDecoration(
        color:        Colors.black.withOpacity(0.65),
        borderRadius: BorderRadius.circular(8),
        border:       Border.all(color: color.withOpacity(0.5)),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        Icon(icon, color: color, size: 14),
        const SizedBox(width: 5),
        Text(label,
            style: TextStyle(
                color: color, fontSize: 11, fontWeight: FontWeight.w600)),
      ]),
    );
  }
}