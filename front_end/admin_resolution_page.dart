import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'socket_service.dart';
import 'api_service.dart';
import 'scan_success_page.dart';

import 'webrtc_config.dart';

enum _Step {
  opening,
  selectingPhone,
  pickingUp,
  scanning,
  trackingAdmin,
  autoStaged,
  unstageNext,
  needsDeposit,
  depositInProgress,
  declaringMissing,
  noQrHandled,
  sessionDone,
  error,
}

class AdminResolutionPage extends StatefulWidget {
  final String password;
  final List<dynamic> mismatches;
  final RTCPeerConnection? inheritedPeerConnection;

  const AdminResolutionPage({
    super.key,
    required this.password,
    required this.mismatches,
    this.inheritedPeerConnection,
  });

  @override
  State<AdminResolutionPage> createState() => _AdminResolutionPageState();
}

class _AdminResolutionPageState extends State<AdminResolutionPage>
    with SingleTickerProviderStateMixin {
  final _socket = SocketService();

  Map<String, int> _mismatchMap = {};
  List<String>     _remaining   = [];
  List<String>     _staged      = [];
  String?          _sessionId;
  final Set<String> _visitedPids = {};

  _Step   _step          = _Step.opening;
  int?    _currentLid;
  String? _currentPid;
  int?    _expectedLid;
  bool    _sameSlot      = false;
  String? _errorText;
  String? _scanError;
  bool    _qrVisible     = true;

  String? _needsDepositPid;

  Map<String, dynamic>? _summary;
  bool _evidenceKept     = false;
  bool _forceClosed      = false;
  bool _sessionCompleted = false;

  Timer? _openingTimer;
  bool   _openingTimedOut = false;

  bool   _pendingServer = false;
  Timer? _serverTimer;

  bool _sessionWasOpened = false;

  bool   _noQrButtonVisible = false;
  Timer? _noQrButtonTimer;
  int    _noQrButtonDelayS  = 25;

  static const Duration _kCameraIdleTimeout = Duration(seconds: 60);
  Timer? _cameraIdleTimer;

  late AnimationController _cardAnim;
  late Animation<Offset>   _cardSlide;

  final RTCVideoRenderer _renderer = RTCVideoRenderer();
  RTCPeerConnection?     _pc;
  bool _videoConnected  = false;
  bool _disposed        = false;
  bool _isReconnecting  = false;

  // Opt #28: typed stream subscriptions — all cancelled in dispose()
  final List<StreamSubscription> _subs = [];

  // ── Camera idle helpers ───────────────────────────────────────────────────

  void _scheduleCameraIdle() {
    _cameraIdleTimer?.cancel();
    _cameraIdleTimer = Timer(_kCameraIdleTimeout, _onCameraIdleTimeout);
  }

  void _cancelCameraIdle() {
    _cameraIdleTimer?.cancel();
    _cameraIdleTimer = null;
  }

  void _onCameraIdleTimeout() {
    if (_disposed || !mounted) return;
    if (_step == _Step.selectingPhone || _step == _Step.pickingUp) {
      _pc?.onTrack            = null;
      _pc?.onConnectionState  = null;
      _pc?.close();
      _pc = null;
      if (mounted) setState(() => _videoConnected = false);
    }
  }

  void _ensureCameraConnected() {
    if (!_videoConnected && !_isReconnecting && !_disposed && _pc == null) {
      _startVideo();
    }
  }

  // ── Display helpers ───────────────────────────────────────────────────────

  static String _slotLabel(int? lid, {int? row, int? col}) {
    final n = (lid ?? 0) + 1;
    if (row != null && col != null) return 'slot $n (row $row, col $col)';
    return 'slot $n';
  }

  static String _displayPid(String? pid) {
    if (pid == null) return '?';
    if (pid.startsWith('unknown-')) return 'Unidentified object';
    return pid;
  }

  // ── Init ──────────────────────────────────────────────────────────────────

  @override
  void initState() {
    super.initState();
    _mismatchMap = {
      for (final m in widget.mismatches)
        m[0].toString(): (m[1] as num).toInt()
    };
    _remaining = List<String>.from(_mismatchMap.keys);

    _cardAnim  = AnimationController(
        vsync: this, duration: const Duration(milliseconds: 350));
    _cardSlide = Tween<Offset>(
      begin: const Offset(0, 1),
      end:   Offset.zero,
    ).animate(CurvedAnimation(parent: _cardAnim, curve: Curves.easeOutCubic));

    _renderer.initialize().then((_) => _initVideo());
    _registerCallbacks(); // Opt #28
    _startOpeningTimer();

    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_disposed) return;
      _cardAnim.forward(from: 0);
      _socket.adminSessionStart(widget.password);
    });
  }

  // ── Opt #28: stream subscriptions ────────────────────────────────────────
  void _registerCallbacks() {
    _socket.connect(); // idempotent

    _subs.add(_socket.onAdminSessionOpened.listen((data) {
      if (!mounted) return;
      _openingTimer?.cancel();
      _serverResponded();
      _sessionWasOpened = true;
      _sessionId        = data['session_id'];
      _noQrButtonDelayS =
          (data['no_qr_button_delay_s'] as num?)?.toInt() ?? 25;
      final List raw = data['mismatches'] ?? [];
      _mismatchMap = {
        for (final m in raw) m['pid'].toString(): m['expected_lid'] as int
      };
      _remaining = List<String>.from(_mismatchMap.keys);
      _cardAnim.forward(from: 0);
      _autoSelect();
    }));

    _subs.add(_socket.onAdminSessionError.listen((data) {
      if (!mounted) return;
      _openingTimer?.cancel();
      _serverResponded();
      final msg = data['message'] ?? 'session_error';
      if (msg == 'no_active_mismatches') {
        Future.delayed(const Duration(milliseconds: 600), () {
          if (!mounted || _disposed || _step != _Step.opening) return;
          _startOpeningTimer();
          _socket.adminSessionStart(widget.password);
        });
        return;
      }
      setState(() {
        _step      = _Step.error;
        _errorText = data['message'] ?? 'Session error';
      });
    }));

    _subs.add(_socket.onAdminRemoveOk.listen((data) {
      if (!mounted) return;
      _serverResponded();
      _noQrButtonVisible = false;
      _noQrButtonTimer?.cancel();
      final delayS =
          (data['no_qr_button_delay_s'] as num?)?.toInt() ?? _noQrButtonDelayS;
      _noQrButtonTimer = Timer(Duration(seconds: delayS), () {
        if (!mounted || _step != _Step.scanning) return;
        setState(() => _noQrButtonVisible = true);
      });
      setState(() { _step = _Step.scanning; _scanError = null; });
    }));

    _subs.add(_socket.onAdminQrResult.listen((data) {
      if (!mounted) return;
      _serverResponded();
      _noQrButtonTimer?.cancel();
      final pid      = data['pid'].toString();
      final needsDep = data['needs_deposit'] == true;
      _currentPid  = pid;
      _sameSlot    = data['same_slot'] == true;
      _expectedLid = data['expected_lid'] != null
          ? int.parse(data['expected_lid'].toString())
          : null;
      if (needsDep) {
        _needsDepositPid = pid;
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (mounted) _startDepositForNeedsDeposit();
        });
      }
    }));

    _subs.add(_socket.onAdminNoQrResult.listen((_) {
      if (!mounted) return;
      _serverResponded();
      _noQrButtonTimer?.cancel();
      setState(() { _step = _Step.noQrHandled; _currentPid = null; });
    }));

    _subs.add(_socket.onAdminAutoStaged.listen((data) {
      if (!mounted) return;
      _serverResponded();
      final pid         = data['pid'].toString();
      final destLid     = (data['dest_lid'] as num?)?.toInt();
      final blockingPid = data['blocking_pid']?.toString();
      _staged.add(pid);
      _remaining = List<String>.from(data['remaining'] ?? _remaining);
      setState(() {
        _step        = _Step.autoStaged;
        _scanError   = null;
        _currentPid  = null;
        _currentLid  = destLid;
        _expectedLid = destLid;
      });
      Future.delayed(const Duration(milliseconds: 400), () {
        if (!mounted) return;
        if (blockingPid != null && _mismatchMap.containsKey(blockingPid)) {
          _selectPhone(blockingPid);
        } else if (destLid != null) {
          final byLid = _remaining.firstWhere(
              (p) => _mismatchMap[p] == destLid, orElse: () => '');
          if (byLid.isNotEmpty) _selectPhone(byLid); else _autoSelect();
        } else {
          _autoSelect();
        }
      });
    }));

    _subs.add(_socket.onAdminPlaceResult.listen((data) {
      if (!mounted) return;
      _serverResponded();
      if (_currentPid != null) _remaining.remove(_currentPid);
      _currentPid = null;
      _sameSlot   = false;
      _remaining  = List<String>.from(data['remaining'] ?? []);
      _staged     = List<String>.from(data['staged']    ?? []);
      if (_staged.isNotEmpty) {
        setState(() => _step = _Step.unstageNext);
        _scheduleCameraIdle();
      } else if (_remaining.isEmpty) {
        _socket.adminSessionClose();
      } else {
        _scheduleCameraIdle();
        _autoSelect();
      }
    }));

    _subs.add(_socket.onAdminMissingResult.listen((data) {
      if (!mounted) return;
      _serverResponded();
      _remaining.remove(data['pid'].toString());
      if (_staged.isNotEmpty) {
        setState(() => _step = _Step.unstageNext);
        _scheduleCameraIdle();
      } else if (_remaining.isEmpty) {
        _socket.adminSessionClose();
      } else {
        _scheduleCameraIdle();
        _autoSelect();
      }
    }));

    _subs.add(_socket.onAdminSessionClosed.listen((data) {
      if (!mounted) return;
      _serverResponded();
      _cancelCameraIdle();
      _noQrButtonTimer?.cancel();
      _sessionCompleted = true;
      _summary          = data['summary'];
      _evidenceKept     = data['evidence_kept'] == true;
      _forceClosed      = data['force_closed']  == true;
      setState(() => _step = _Step.sessionDone);
      _cardAnim.forward(from: 0);
    }));

    _subs.add(_socket.onAdminOperationError.listen((data) {
      if (!mounted) return;
      _serverResponded();
      final msg = data['message']?.toString() ?? 'Unknown error';
      if (_step == _Step.trackingAdmin) {
        setState(() {
          _step      = _Step.pickingUp;
          _scanError = _trackingFailureMsg(data['reason']?.toString() ?? msg);
        });
        _scheduleCameraIdle();
        return;
      }
      if (_step == _Step.scanning) {
        setState(() { _noQrButtonVisible = true; _scanError = _friendlyError(msg); });
        return;
      }
      setState(() => _scanError = _friendlyError(msg));
    }));

    _subs.add(_socket.onAdminStepCancelled.listen((_) {
      if (!mounted) return;
      _serverResponded();
      setState(() {
        _currentPid  = null;
        _currentLid  = null;
        _sameSlot    = false;
        _scanError   = null;
        _step        = _Step.selectingPhone;
      });
      _scheduleCameraIdle();
      _cardAnim.forward(from: 0);
    }));

    _subs.add(_socket.onTrackingStarted.listen((_) {
      if (!mounted) return;
      _cancelCameraIdle();
      setState(() { _step = _Step.trackingAdmin; _qrVisible = true; _scanError = null; });
    }));

    _subs.add(_socket.onTrackingUpdate.listen((data) {
      if (!mounted) return;
      setState(() => _qrVisible = data['qr_visible'] as bool? ?? true);
    }));

    _subs.add(_socket.onTrackingFailed.listen((data) {
      if (!mounted) return;
      final reason = data['reason'] as String? ?? '';
      setState(() { _step = _Step.pickingUp; _scanError = _trackingFailureMsg(reason); });
      _scheduleCameraIdle();
    }));
  }

  String _trackingFailureMsg(String reason) {
    const map = {
      'qr_lost':               'QR code disappeared before the phone reached the slot. Retry.',
      'out_of_frame':          'Phone left the camera view. Move directly toward the slot and retry.',
      'timeout':               'Placement timed out. Retry.',
      'detect_timeout':        'Phone not detected. Make sure it enters the camera view and retry.',
      'phone_not_in_slot':     'Phone not detected in slot by internal camera. Retry.',
      'insertion_timeout':     'Phone did not complete insertion in time. Retry.',
      'stabilization_timeout': 'Phone did not settle in time. Hold flat and retry.',
      'tracker_lost':          'Tracking lost before phone reached slot. Move steadily.',
      'error':                 'Tracker error. Retry.',
    };
    return map[reason] ?? 'Tracking failed ($reason). Retry.';
  }

  String _friendlyError(String code) {
    const map = {
      'qr_not_detected':      'No QR code found. Hold it steady and try again.',
      'pid_not_found':        'This QR code is not in the system.',
      'camera_error':         'Camera error. Make sure the top camera is connected.',
      'scan_error':           'Scan failed. Try again.',
      'qr_already_confirmed': 'QR already confirmed for this phone.',
      'no_object_removed':    'Remove the phone from its slot first.',
      'step_lock_violated':   'Place or stage the current phone before picking up another.',
      'db_update_failed':     'Database update failed. Try again.',
    };
    return map[code] ?? 'Error: $code';
  }

  // ── Actions ───────────────────────────────────────────────────────────────

  bool get _canDeclareCurrentMissing {
    if (_currentLid == null) return false;
    final currentPid = _remaining.firstWhere(
        (p) => _mismatchMap[p] == _currentLid, orElse: () => '');
    if (currentPid.isEmpty) return false;
    return _remaining
        .where((p) => p != currentPid)
        .every((p) => _visitedPids.contains(p));
  }

  Future<void> _onDeclareCurrentMissing() async {
    final pid = _remaining.firstWhere(
        (p) => _mismatchMap[p] == _currentLid, orElse: () => '');
    if (pid.isEmpty) return;
    setState(() => _step = _Step.declaringMissing);
    final confirm = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        title: const Text("Can't find phone?"),
        content: Text(
          "You've checked all other mismatches.\n\n"
          'Declare ${_displayPid(pid)} as missing?\n'
          'Its DB record will be withdrawn and evidence recorded permanently.',
        ),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel')),
          ElevatedButton(
            style: ElevatedButton.styleFrom(backgroundColor: Colors.red),
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Declare Missing'),
          ),
        ],
      ),
    );
    if (!mounted) return;
    if (confirm == true) {
      _socket.adminDeclareMissing(pid);
    } else {
      setState(() => _step = _Step.pickingUp);
    }
  }

  void _autoSelect() {
    if (_staged.isNotEmpty) { setState(() => _step = _Step.unstageNext); return; }
    if (_remaining.isNotEmpty) { _selectPhone(_remaining.first); return; }
    _socket.adminSessionClose();
  }

  void _selectPhone(String pid) {
    _cancelCameraIdle();
    _ensureCameraConnected();
    final lid = _mismatchMap[pid] ?? 0;
    _visitedPids.add(pid);
    setState(() {
      _currentPid  = null;
      _currentLid  = lid;
      _expectedLid = lid;
      _sameSlot    = false;
      _scanError   = null;
      _step        = _Step.pickingUp;
    });
    _cardAnim.forward(from: 0);
  }

  void _onPickedUp() {
    if (_currentLid == null) return;
    _cancelCameraIdle();
    _waitForServer();
    _socket.adminRemovePhone(_currentLid!);
  }

  void _onNoQrCode() {
    _noQrButtonTimer?.cancel();
    _waitForServer();
    _socket.adminNoQrFound();
  }

  void _onUnstageNext() {
    if (_staged.isEmpty) return;
    _cancelCameraIdle();
    _waitForServer();
    _socket.adminUnstagePhone(_staged.first);
  }

  void _onNextAfterInfo() {
    setState(() => _scanError = null);
    if (_remaining.isEmpty && _staged.isEmpty) {
      _socket.adminSessionClose();
    } else {
      _autoSelect();
    }
  }

  void _startDepositForNeedsDeposit() {
    final pid = _needsDepositPid;
    if (pid == null) return;
    setState(() => _step = _Step.depositInProgress);
    _socket.deposit(pid);
    showModalBottomSheet(
      context:            context,
      isDismissible:      false,
      enableDrag:         false,
      isScrollControlled: true,
      backgroundColor:    Colors.transparent,
      shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.vertical(top: Radius.circular(20))),
      builder: (_) => DVWBottomSheet(
        pid:           pid,
        isDeposit:     true,
        socketService: _socket,
        onComplete:    () {
          if (mounted) {
            _remaining.remove(pid);
            _needsDepositPid = null;
            _onNextAfterInfo();
          }
        },
      ),
    ).then((_) {
      if (mounted && _step == _Step.depositInProgress) {
        setState(() { _needsDepositPid = null; _step = _Step.selectingPhone; });
        _autoSelect();
      }
    });
  }

  void _onChangePhone() {
    _noQrButtonTimer?.cancel();
    if (_step == _Step.scanning) {
      _waitForServer();
      _socket.adminCancelStep();
    } else {
      setState(() {
        _currentPid  = null;
        _currentLid  = null;
        _sameSlot    = false;
        _scanError   = null;
        _step        = _Step.selectingPhone;
      });
      _scheduleCameraIdle();
      _cardAnim.forward(from: 0);
    }
  }

  // ── Timers ────────────────────────────────────────────────────────────────

  void _startOpeningTimer() {
    _openingTimer = Timer(const Duration(seconds: 8), () {
      if (!mounted || _step != _Step.opening) return;
      setState(() => _openingTimedOut = true);
    });
  }

  void _retrySession() {
    setState(() => _openingTimedOut = false);
    _openingTimer?.cancel();
    _startOpeningTimer();
    _socket.adminSessionStart(widget.password);
  }

  void _waitForServer() {
    _serverTimer?.cancel();
    setState(() { _pendingServer = true; _scanError = null; });
    _serverTimer = Timer(const Duration(seconds: 30), () {
      if (!mounted || !_pendingServer) return;
      setState(() {
        _pendingServer = false;
        _scanError     = 'No response from server. Check your connection.';
      });
    });
  }

  void _serverResponded() {
    _serverTimer?.cancel();
    _pendingServer = false;
  }

  // ── Video ─────────────────────────────────────────────────────────────────

  Future<void> _initVideo() async {
    if (_disposed) return;
    if (widget.inheritedPeerConnection != null) {
      _pc = widget.inheritedPeerConnection;
      _pc!.onTrack = (event) {
        if (_disposed || !mounted) return;
        if (event.streams.isNotEmpty) {
          setState(() {
            _renderer.srcObject = event.streams[0];
            _videoConnected     = true;
          });
        }
      };
      _pc!.onConnectionState = _handleConnectionState;
      try {
        final receivers = await _pc!.getReceivers();
        for (final r in receivers) {
          if (r.track?.kind == 'video') {
            final stream = await createLocalMediaStream('admin-adopted');
            await stream.addTrack(r.track!);
            if (!_disposed && mounted) {
              setState(() {
                _renderer.srcObject = stream;
                _videoConnected     = true;
              });
            }
            break;
          }
        }
      } catch (_) {}
    } else {
      await _startVideo();
    }
  }

  void _handleConnectionState(RTCPeerConnectionState state) async {
    if (_disposed || _isReconnecting) return;
    if (state == RTCPeerConnectionState.RTCPeerConnectionStateDisconnected ||
        state == RTCPeerConnectionState.RTCPeerConnectionStateFailed) {
      _isReconnecting = true;
      if (mounted) setState(() => _videoConnected = false);
      await _pc?.close();
      _pc = null;
      await Future.delayed(const Duration(seconds: 2));
      if (!_disposed) {
        _renderer.srcObject = null;
        await _startVideo();
      }
      _isReconnecting = false;
    }
  }

  Future<void> _startVideo() async {
    if (_disposed) return;
    try {
      await ApiService.cancelAdmin();
      _pc = await createPeerConnection(WebRTCConfig.iceConfig);
      _pc!.onTrack = (event) {
        if (_disposed || !mounted) return;
        if (event.streams.isNotEmpty) {
          setState(() {
            _renderer.srcObject = event.streams[0];
            _videoConnected     = true;
          });
        }
      };
      _pc!.onConnectionState = _handleConnectionState;
      final offer = await _pc!.createOffer(WebRTCConfig.videoOfferConstraints);
      await _pc!.setLocalDescription(offer);
      final sdp = await ApiService.sendOffer(offer.sdp!, mode: 'admin');
      if (sdp != null && !_disposed) {
        await _pc!.setRemoteDescription(RTCSessionDescription(sdp, 'answer'));
      }
    } catch (_) {
      if (mounted) setState(() => _videoConnected = false);
    }
  }

  // ── Lifecycle ─────────────────────────────────────────────────────────────

  @override
  void dispose() {
    _disposed = true;
    _cameraIdleTimer?.cancel();
    _openingTimer?.cancel();
    _serverTimer?.cancel();
    _noQrButtonTimer?.cancel();
    _cardAnim.dispose();
    // Opt #28: cancel all stream subscriptions — no leaks
    for (final s in _subs) s.cancel();
    _pc?.onTrack            = null;
    _pc?.onConnectionState  = null;
    _pc?.close();
    _renderer.srcObject = null;
    _renderer.dispose();
    ApiService.cancelAdmin();
    super.dispose();
  }

  // ── Build ─────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    final canPop = _step == _Step.sessionDone || _step == _Step.error;
    return PopScope(
      canPop: canPop,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop) {
          ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
            content:  Text('Use the button below to close the session properly.'),
            duration: Duration(seconds: 2),
          ));
        }
      },
      child: Scaffold(
        backgroundColor: Colors.black,
        body: SafeArea(
          child: Column(children: [
            Expanded(child: Stack(fit: StackFit.expand, children: [
              _buildCamera(),
              _buildTopBar(),
            ])),
            SlideTransition(
              position: _cardSlide,
              child:    _buildCard(),
            ),
          ]),
        ),
      ),
    );
  }

  Widget _buildCamera() {
    // Opt #30: RepaintBoundary isolates 30fps video repaints
    return _videoConnected && _renderer.srcObject != null
        ? RepaintBoundary(
            child: RTCVideoView(
              _renderer,
              objectFit: RTCVideoViewObjectFit.RTCVideoViewObjectFitContain,
            ),
          )
        : Container(
            color: Colors.black,
            child: Center(child: Column(mainAxisSize: MainAxisSize.min, children: [
              const SizedBox(width: 28, height: 28,
                  child: CircularProgressIndicator(
                      strokeWidth: 2.5, color: Colors.white38)),
              const SizedBox(height: 12),
              const Text('Connecting top camera...',
                  style: TextStyle(color: Colors.white38, fontSize: 13)),
            ])),
          );
  }

  Widget _buildTopBar() {
    final total = _mismatchMap.length;
    final done  = total - _remaining.length;
    return _TopProgressBar(
      total:       total,
      done:        done,
      isRecording: _step == _Step.scanning || _step == _Step.trackingAdmin,
    );
  }

  Widget _buildCard() {
    final showEndBtn = _step != _Step.opening &&
        _step != _Step.sessionDone &&
        _step != _Step.error &&
        _step != _Step.depositInProgress;

    return Container(
      decoration: BoxDecoration(
        color:        const Color(0xFF1C1C1E),
        borderRadius: const BorderRadius.vertical(top: Radius.circular(24)),
        boxShadow: [BoxShadow(
            color:       Colors.black.withOpacity(0.6),
            blurRadius:  24,
            spreadRadius: 4)],
      ),
      child: Column(mainAxisSize: MainAxisSize.min, children: [
        const SizedBox(height: 10),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: SizedBox(
            height: 28,
            child: Stack(
              alignment: Alignment.center,
              children: [
                Center(
                  child: Container(
                    width: 36, height: 4,
                    decoration: BoxDecoration(
                        color:        Colors.white24,
                        borderRadius: BorderRadius.circular(2)),
                  ),
                ),
                if (showEndBtn)
                  Positioned(right: 0, child: _forceCloseButton()),
              ],
            ),
          ),
        ),
        const SizedBox(height: 4),
        SingleChildScrollView(
          padding: EdgeInsets.fromLTRB(
              20, 12, 20,
              MediaQuery.of(context).viewInsets.bottom + 24),
          child: _buildCardContent(),
        ),
      ]),
    );
  }

  // ── Card content (unchanged logic, trimmed for space) ─────────────────────

  Widget _buildCardContent() {
    switch (_step) {
      case _Step.opening:        return _openingContent();
      case _Step.selectingPhone: return _phoneSelectContent();
      case _Step.pickingUp:      return _pickingUpContent();
      case _Step.scanning:       return _scanningContent();
      case _Step.trackingAdmin:  return _trackingContent();
      case _Step.autoStaged:     return _stepContent(
          icon: Icons.hourglass_top, color: Colors.amber,
          title: 'Phone staged — loading next step...',
          subtitle: 'The system detected the phone in the staging zone.',
          loading: true, actions: const []);
      case _Step.unstageNext:    return _unstageContent();
      case _Step.depositInProgress: return _stepContent(
          icon: Icons.move_to_inbox_outlined, color: Colors.indigoAccent,
          title: 'Deposit in progress...',
          subtitle: 'Follow the instructions in the panel below.',
          loading: true, actions: const []);
      case _Step.needsDeposit:   return _stepContent(
          icon: Icons.move_to_inbox_outlined, color: Colors.indigoAccent,
          title: 'Starting deposit...',
          subtitle: 'Preparing deposit for the phone found in this slot.',
          loading: true, actions: const []);
      case _Step.noQrHandled:    return _stepContent(
          icon: Icons.hourglass_top, color: Colors.grey,
          title: 'Waiting for confirmation...',
          subtitle: 'Confirm the declaration in the dialog above.',
          loading: true, actions: const []);
      case _Step.declaringMissing: return _stepContent(
          icon: Icons.search_off, color: Colors.redAccent,
          title: 'Declaring phone as missing...',
          subtitle: 'Updating the database and saving evidence. Please wait.',
          loading: true, actions: const []);
      case _Step.sessionDone:    return _doneContent();
      case _Step.error:          return _errorContent();
    }
  }

  Widget _openingContent() {
    if (_openingTimedOut) {
      return Column(mainAxisSize: MainAxisSize.min, children: [
        const Icon(Icons.wifi_off, color: Colors.orange, size: 40),
        const SizedBox(height: 12),
        const Text('No response from server',
            style: TextStyle(fontSize: 17, fontWeight: FontWeight.bold, color: Colors.white)),
        const SizedBox(height: 8),
        const Text(
          'Check that admin handlers are registered and the slot monitor is running.',
          textAlign: TextAlign.center,
          style: TextStyle(color: Colors.white54, fontSize: 13),
        ),
        const SizedBox(height: 20),
        _primaryBtn('Retry', Colors.orange, _retrySession),
        const SizedBox(height: 8),
        TextButton(
            onPressed: () => Navigator.of(context).pop(_sessionWasOpened ? false : null),
            child: const Text('Cancel', style: TextStyle(color: Colors.white54))),
      ]);
    }
    return const Column(mainAxisSize: MainAxisSize.min, children: [
      SizedBox(height: 8),
      SizedBox(width: 28, height: 28,
          child: CircularProgressIndicator(strokeWidth: 2.5, color: Colors.white54)),
      SizedBox(height: 16),
      Text('Opening session...',
          style: TextStyle(fontSize: 17, fontWeight: FontWeight.bold, color: Colors.white)),
      SizedBox(height: 6),
      Text('Authenticating and loading mismatch list.',
          style: TextStyle(color: Colors.white54, fontSize: 13)),
      SizedBox(height: 8),
    ]);
  }

  Widget _phoneSelectContent() {
    final hasStaged = _staged.isNotEmpty;
    return Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
      Row(children: [
        const Icon(Icons.swap_horiz, color: Colors.deepOrangeAccent, size: 20),
        const SizedBox(width: 8),
        const Expanded(child: Text('Choose which phone to handle',
            style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold, color: Colors.white))),
        TextButton(
          style: TextButton.styleFrom(padding: EdgeInsets.zero, minimumSize: Size.zero),
          onPressed: _autoSelect,
          child: const Text('Auto', style: TextStyle(color: Colors.deepOrangeAccent, fontSize: 13, fontWeight: FontWeight.w600)),
        ),
      ]),
      const SizedBox(height: 4),
      Text(
        hasStaged ? 'Retrieve staged phones before picking new ones.' : 'Tap any phone to go straight to pick-up.',
        style: const TextStyle(color: Colors.white54, fontSize: 13),
      ),
      const SizedBox(height: 14),
      if (hasStaged) ...[
        const Text('STAGED', style: TextStyle(color: Colors.tealAccent, fontSize: 11, fontWeight: FontWeight.w700, letterSpacing: 1.2)),
        const SizedBox(height: 6),
        ..._staged.map((pid) => _phoneChip(pid: pid, lid: _mismatchMap[pid] ?? 0, color: Colors.teal, icon: Icons.unarchive_outlined, onTap: () => _socket.adminUnstagePhone(pid))),
        const SizedBox(height: 12),
      ],
      if (_remaining.isNotEmpty) ...[
        if (hasStaged) ...[
          const Text('REMAINING', style: TextStyle(color: Colors.white38, fontSize: 11, fontWeight: FontWeight.w700, letterSpacing: 1.2)),
          const SizedBox(height: 6),
        ],
        ..._remaining.map((pid) {
          final isDefault = pid == _remaining.first && !hasStaged;
          return _phoneChip(pid: pid, lid: _mismatchMap[pid] ?? 0, color: isDefault ? Colors.deepOrangeAccent : Colors.white54, icon: isDefault ? Icons.smartphone : Icons.smartphone_outlined, onTap: () => _selectPhone(pid));
        }),
      ],
      const SizedBox(height: 8),
    ]);
  }

  Widget _pickingUpContent() => _stepContent(
    icon: Icons.pan_tool_alt_outlined, color: Colors.orange,
    title: 'Pick up the object from ${_slotLabel(_currentLid)}',
    subtitle: 'Once you have it in hand, tap the button. QR scan will start automatically.',
    error: _scanError,
    actions: [
      _primaryBtn("I've picked it up", Colors.orange, _onPickedUp),
      _changePhoneBtn(),
      const SizedBox(height: 4),
      Opacity(
        opacity: (_canDeclareCurrentMissing && !_pendingServer) ? 1.0 : 0.35,
        child: TextButton.icon(
          icon:  const Icon(Icons.search_off, size: 16, color: Colors.redAccent),
          label: Text(
            _canDeclareCurrentMissing ? "Can't find it — declare missing" : "Can't find it (check all other phones first)",
            style: const TextStyle(color: Colors.redAccent, fontSize: 13),
          ),
          onPressed: (_canDeclareCurrentMissing && !_pendingServer) ? _onDeclareCurrentMissing : null,
        ),
      ),
    ],
  );

  Widget _scanningContent() => _stepContent(
    icon: Icons.qr_code_scanner, color: Colors.blueAccent,
    title: 'Scanning for QR code...',
    subtitle: 'Hold the QR code under the camera and keep it still.',
    loading: !_noQrButtonVisible, error: _scanError,
    actions: [
      if (_noQrButtonVisible)
        OutlinedButton.icon(
          icon:  const Icon(Icons.hide_image_outlined, size: 18),
          label: const Text('No QR code on this object'),
          style: OutlinedButton.styleFrom(
              minimumSize: const Size(double.infinity, 48),
              side: const BorderSide(color: Colors.white30)),
          onPressed: _pendingServer ? null : _onNoQrCode,
        ),
      _changePhoneBtn(),
    ],
  );

  Widget _trackingContent() => _stepContent(
    icon: Icons.my_location_outlined, color: Colors.greenAccent,
    title: 'Move phone to ${_slotLabel(_expectedLid)}',
    subtitle: 'Move it directly to the slot — or into a staging zone if the slot is occupied.',
    error: _scanError,
    actions: [_TrackingQrBadge(qrVisible: _qrVisible)],
  );

  Widget _unstageContent() {
    final pid = _staged.isNotEmpty ? _staged.first : '?';
    final lid = _mismatchMap[pid] ?? 0;
    return _stepContent(
      icon: Icons.unarchive_outlined, color: Colors.tealAccent,
      title: 'Retrieve ${_displayPid(pid)} from staging',
      subtitle: 'Pick it up from the staging zone. Tracking to slot ${lid + 1} will start automatically.',
      error: _scanError,
      actions: [_primaryBtn("I've retrieved it from staging", Colors.teal, _onUnstageNext)],
    );
  }

  Widget _doneContent() {
    final resolved   = (_summary?['resolved']         as List?)?.length ?? 0;
    final missing    = (_summary?['declared_missing'] as List?)?.length ?? 0;
    final depositing = (_summary?['needs_deposit']    as List?)?.length ?? 0;
    final unresolved = (_summary?['unresolved']       as List?)?.length ?? 0;

    final icon      = _forceClosed ? Icons.cancel_outlined   : Icons.check_circle_outline;
    final iconColor = _forceClosed ? Colors.orange            : Colors.greenAccent;
    final title     = _forceClosed ? 'Session ended early'   : 'All done! Have a nice day';

    return Column(mainAxisSize: MainAxisSize.min, children: [
      Icon(icon, color: iconColor, size: 48),
      const SizedBox(height: 14),
      Text(title, textAlign: TextAlign.center,
          style: const TextStyle(fontSize: 20, fontWeight: FontWeight.bold, color: Colors.white)),
      const SizedBox(height: 14),
      Wrap(spacing: 8, runSpacing: 6, alignment: WrapAlignment.center, children: [
        if (resolved   > 0) _summaryChip('$resolved resolved',     Colors.green),
        if (missing    > 0) _summaryChip('$missing missing',       Colors.red),
        if (depositing > 0) _summaryChip('$depositing to deposit', Colors.indigo),
        if (unresolved > 0) _summaryChip('$unresolved unresolved', Colors.orange),
      ]),
      if (_forceClosed) ...[
        const SizedBox(height: 12),
        _warningBox(Colors.orange, Icons.folder_open_outlined,
            'Evidence saved as NotFullyResolved. Unresolved mismatches will re-alarm.'),
      ] else if (_evidenceKept) ...[
        const SizedBox(height: 12),
        _warningBox(Colors.orange, Icons.warning_amber_outlined,
            'Evidence kept — anomalies were recorded.'),
      ],
      const SizedBox(height: 24),
      _primaryBtn('Close', _forceClosed ? Colors.deepOrangeAccent : Colors.green,
          () => Navigator.of(context).pop(_forceClosed ? false : true)),
    ]);
  }

  Widget _errorContent() => Column(mainAxisSize: MainAxisSize.min, children: [
    const Icon(Icons.error_outline, color: Colors.redAccent, size: 44),
    const SizedBox(height: 14),
    const Text('Session error',
        style: TextStyle(fontSize: 17, fontWeight: FontWeight.bold, color: Colors.white)),
    const SizedBox(height: 8),
    Text(_errorText ?? 'Something went wrong.',
        textAlign: TextAlign.center,
        style: const TextStyle(color: Colors.white54, fontSize: 13)),
    const SizedBox(height: 24),
    _primaryBtn('Close', Colors.grey,
        () => Navigator.of(context).pop(_sessionWasOpened ? false : null)),
  ]);

  // ── Shared sub-widgets ────────────────────────────────────────────────────

  Widget _stepContent({
    required IconData icon, required Color color,
    required String title, required String subtitle,
    String? error, bool loading = false, List<Widget> actions = const [],
  }) => Column(mainAxisSize: MainAxisSize.min, children: [
    if (loading)
      SizedBox(width: 36, height: 36,
          child: CircularProgressIndicator(strokeWidth: 2.5, color: color))
    else
      Icon(icon, color: color, size: 38),
    const SizedBox(height: 14),
    Text(title, textAlign: TextAlign.center,
        style: const TextStyle(fontSize: 17, fontWeight: FontWeight.bold, color: Colors.white)),
    const SizedBox(height: 8),
    Text(subtitle, textAlign: TextAlign.center,
        style: const TextStyle(color: Colors.white54, fontSize: 13, height: 1.45)),
    if (error != null) ...[
      const SizedBox(height: 12),
      Container(
        width: double.infinity,
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        decoration: BoxDecoration(
          color:        Colors.red.withOpacity(0.15),
          border:       Border.all(color: Colors.red.withOpacity(0.4)),
          borderRadius: BorderRadius.circular(10),
        ),
        child: Row(children: [
          const Icon(Icons.error_outline, color: Colors.redAccent, size: 18),
          const SizedBox(width: 10),
          Expanded(child: Text(error,
              style: const TextStyle(color: Colors.redAccent, fontSize: 13))),
        ]),
      ),
    ],
    const SizedBox(height: 20),
    ...actions,
  ]);

  Widget _phoneChip({
    required String pid, required int lid, required Color color,
    required IconData icon, required VoidCallback onTap,
  }) => Padding(
    padding: const EdgeInsets.only(bottom: 8),
    child: InkWell(
      onTap: onTap, borderRadius: BorderRadius.circular(12),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
        decoration: BoxDecoration(
          color:        color.withOpacity(0.12),
          border:       Border.all(color: color.withOpacity(0.35)),
          borderRadius: BorderRadius.circular(12),
        ),
        child: Row(children: [
          Icon(icon, color: color, size: 20),
          const SizedBox(width: 12),
          Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(_displayPid(pid),
                style: TextStyle(color: color, fontSize: 14, fontWeight: FontWeight.w600)),
            Text('Expected in slot ${lid + 1}',
                style: const TextStyle(color: Colors.white54, fontSize: 12)),
          ])),
          Icon(Icons.chevron_right, color: color.withOpacity(0.6), size: 20),
        ]),
      ),
    ),
  );

  Widget _summaryChip(String label, Color color) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 5),
    decoration: BoxDecoration(
        color:        color.withOpacity(0.15),
        borderRadius: BorderRadius.circular(20),
        border:       Border.all(color: color.withOpacity(0.4))),
    child: Text(label,
        style: TextStyle(color: color, fontSize: 13, fontWeight: FontWeight.w600)),
  );

  Widget _warningBox(Color color, IconData icon, String text) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
    decoration: BoxDecoration(
      color:        color.withOpacity(0.15),
      borderRadius: BorderRadius.circular(10),
      border:       Border.all(color: color.withOpacity(0.4)),
    ),
    child: Row(children: [
      Icon(icon, color: color, size: 18),
      const SizedBox(width: 10),
      Expanded(child: Text(text, style: TextStyle(color: color, fontSize: 13))),
    ]),
  );

  Widget _primaryBtn(String label, Color? color, VoidCallback onPressed) =>
      ElevatedButton(
        style: ElevatedButton.styleFrom(
          backgroundColor: color ?? Colors.deepOrangeAccent,
          minimumSize:     const Size(double.infinity, 50),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
          textStyle: const TextStyle(fontSize: 15, fontWeight: FontWeight.w600),
        ),
        onPressed: _pendingServer ? null : onPressed,
        child: _pendingServer
            ? const SizedBox(width: 20, height: 20,
                child: CircularProgressIndicator(strokeWidth: 2.5, color: Colors.white70))
            : Text(label),
      );

  Widget _changePhoneBtn() => Padding(
    padding: const EdgeInsets.only(top: 4),
    child: TextButton.icon(
      icon:      const Icon(Icons.swap_horiz, size: 16, color: Colors.white38),
      label:     const Text('Handle a different phone first',
          style: TextStyle(color: Colors.white38, fontSize: 13)),
      onPressed: _pendingServer ? null : _onChangePhone,
    ),
  );

  Widget _forceCloseButton() => GestureDetector(
    onLongPress: () => _onForceClose(safe: false),
    child: TextButton.icon(
      icon:  const Icon(Icons.close, size: 14, color: Colors.white38),
      label: const Text('End session',
          style: TextStyle(color: Colors.white38, fontSize: 12)),
      style: TextButton.styleFrom(
        padding:       const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        minimumSize:   Size.zero,
        tapTargetSize: MaterialTapTargetSize.shrinkWrap,
      ),
      onPressed: _pendingServer ? null : () => _onForceClose(safe: true),
    ),
  );

  Future<void> _onForceClose({required bool safe}) async {
    final hasPhoneInHand = _step == _Step.scanning || _step == _Step.trackingAdmin;
    final hasStaged      = _staged.isNotEmpty;
    final hasRemaining   = _remaining.isNotEmpty;

    String warningText;
    if (!safe) {
      warningText = 'DEBUG: Force-close regardless of state.\n'
          'All evidence will be saved as NotFullyResolved.';
    } else if (hasPhoneInHand) {
      if (mounted) setState(() => _scanError = 'You have a phone in hand. Put it down first, then close the session.');
      return;
    } else if (hasStaged) {
      if (mounted) setState(() => _scanError = 'There are staged phones. Resolve them first, then close the session.');
      return;
    } else {
      warningText = hasRemaining
          ? 'End session now?\n\n${_remaining.length} mismatch(es) will remain unresolved.\nEvidence will be saved as NotFullyResolved.'
          : 'End session early? All resolved mismatches will be saved.';
    }

    final confirm = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        backgroundColor: const Color(0xFF1C1C1E),
        title: Text(safe ? 'End session' : 'Force close (debug)',
            style: const TextStyle(color: Colors.white)),
        content: Text(warningText,
            style: const TextStyle(color: Colors.white70, fontSize: 13)),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel', style: TextStyle(color: Colors.white54))),
          ElevatedButton(
            style: ElevatedButton.styleFrom(
                backgroundColor: safe ? Colors.deepOrangeAccent : Colors.red),
            onPressed: () => Navigator.pop(context, true),
            child: Text(safe ? 'End session' : 'Force close'),
          ),
        ],
      ),
    );

    if (confirm != true || !mounted) return;
    _cancelCameraIdle();
    _waitForServer();
    _socket.adminForceClose(safe: safe);
  }
}

// ── QR tracking badge ─────────────────────────────────────

class _TrackingQrBadge extends StatelessWidget {
  final bool qrVisible;
  const _TrackingQrBadge({required this.qrVisible});

  @override
  Widget build(BuildContext context) {
    final color = qrVisible ? Colors.green : Colors.orange;
    final icon  = qrVisible ? Icons.qr_code_2 : Icons.qr_code_2_outlined;
    final label = qrVisible
        ? 'QR visible — move phone to destination slot'
        : 'QR not visible — keep QR facing up!';
    return AnimatedContainer(
      duration: const Duration(milliseconds: 300),
      padding:  const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
      decoration: BoxDecoration(
        color:        color.withOpacity(0.12),
        borderRadius: BorderRadius.circular(10),
        border:       Border.all(color: color.withOpacity(0.4)),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        Icon(icon, color: color, size: 20),
        const SizedBox(width: 8),
        Flexible(child: Text(label,
            style: TextStyle(color: color, fontSize: 13, fontWeight: FontWeight.w500))),
      ]),
    );
  }
}

// ── Session progress bar ──────────────────────────────────

class _TopProgressBar extends StatelessWidget {
  final int  total;
  final int  done;
  final bool isRecording;
  const _TopProgressBar({required this.total, required this.done, required this.isRecording});

  static Widget _badge(IconData icon, String label, Color bg) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
    decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(5)),
    child: Row(mainAxisSize: MainAxisSize.min, children: [
      Icon(icon, color: Colors.white, size: 11),
      const SizedBox(width: 4),
      Text(label, style: const TextStyle(color: Colors.white, fontSize: 10, fontWeight: FontWeight.w600)),
    ]),
  );

  @override
  Widget build(BuildContext context) {
    final pct = total > 0 ? done / total : 0.0;
    return Positioned(
      top: 0, left: 0, right: 0,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
        decoration: const BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topCenter, end: Alignment.bottomCenter,
            colors: [Colors.black87, Colors.transparent],
          ),
        ),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            if (isRecording) _badge(Icons.fiber_manual_record, 'REC', Colors.red),
            const SizedBox(width: 8),
            _badge(Icons.videocam_outlined, 'TOP CAM', Colors.white24),
            const Spacer(),
            if (total > 0)
              Text('$done / $total',
                  style: const TextStyle(color: Colors.white70, fontSize: 13)),
          ]),
          if (total > 0) ...[
            const SizedBox(height: 6),
            ClipRRect(
              borderRadius: BorderRadius.circular(2),
              child: LinearProgressIndicator(
                value:           pct,
                minHeight:       3,
                backgroundColor: Colors.white24,
                valueColor:      AlwaysStoppedAnimation(
                    done >= total ? Colors.green : Colors.deepOrangeAccent),
              ),
            ),
          ],
        ]),
      ),
    );
  }
}