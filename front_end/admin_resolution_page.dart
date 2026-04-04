import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'socket_service.dart';
import 'api_service.dart';

enum _Step {
  opening,
  selectingPhone,
  pickingUp,
  awaitingScan,
  scanning,
  placing,
  stagingNeeded,
  unstageNext,
  noQrHandled,
  needsDeposit,
  declaringMissing,
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
  List<String> _remaining = [];
  List<String> _staged = [];
  String? _sessionId;
  final Set<String> _visitedPids = {};

  _Step _step = _Step.opening;
  int? _currentLid;
  String? _currentPid;
  int? _expectedLid;
  bool _sameSlot = false;
  String? _infoMessage;
  String? _errorText;
  String? _scanError;

  Map<String, dynamic>? _summary;
  bool _evidenceKept = false;
  bool _sessionCompleted = false;

  Timer? _openingTimer;
  bool _openingTimedOut = false;

  bool _pendingServer = false;
  Timer? _serverTimer;

  late AnimationController _cardAnim;
  late Animation<Offset> _cardSlide;

  final RTCVideoRenderer _renderer = RTCVideoRenderer();
  RTCPeerConnection? _pc;
  bool _videoConnected = false;
  bool _disposed = false;
  bool _isReconnecting = false;

  // ── Display helpers ───────────────────────────────────

  /// "slot N" — lid is 0-based.
  static int _displaySlot(int? lid) => (lid ?? 0) + 1;

  /// "slot N (row R, col C)" when row/col are known.
  static String _slotLabel(int? lid, {int? row, int? col}) {
    final n = (lid ?? 0) + 1;
    if (row != null && col != null) return 'slot $n (row $row, col $col)';
    return 'slot $n';
  }

  static String _displayPid(String? pid) {
    if (pid == null) return '?';
    if (pid.startsWith('unknown-')) return 'Unidentified object';
    if (pid.length > 18) return '…${pid.substring(pid.length - 12)}';
    return pid;
  }

  // ── Init ──────────────────────────────────────────────

  @override
  void initState() {
    super.initState();
    _mismatchMap = {
      for (final m in widget.mismatches)
        m[0].toString(): (m[1] as num).toInt()
    };
    _remaining = List<String>.from(_mismatchMap.keys);

    _cardAnim = AnimationController(
        vsync: this, duration: const Duration(milliseconds: 350));
    _cardSlide = Tween<Offset>(
      begin: const Offset(0, 1),
      end: Offset.zero,
    ).animate(CurvedAnimation(parent: _cardAnim, curve: Curves.easeOutCubic));

    _renderer.initialize().then((_) => _initVideo());
    _registerCallbacks();
    _startOpeningTimer();

    // Slide the card in immediately after the first frame so the opening
    // spinner is always visible regardless of server response timing.
    // Previously the card started at Offset(0,1) (fully off-screen) and only
    // animated in inside onAdminSessionOpened — but if that event arrived
    // before Flutter's first frame was built, the setState was silently
    // dropped and the card stayed invisible forever (black screen).
    //
    // adminSessionStart is deferred to the same callback so the server reply
    // can never race ahead of _registerCallbacks() assigning the handler
    // pointer.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_disposed) return;
      _cardAnim.forward(from: 0);
      _socket.adminSessionStart(widget.password);
    });
  }

  // ── Video ─────────────────────────────────────────────

  Future<void> _initVideo() async {
    if (_disposed) return;
    if (widget.inheritedPeerConnection != null) {
      _pc = widget.inheritedPeerConnection;
      _pc!.onTrack = (event) {
        if (_disposed || !mounted) return;
        if (event.streams.isNotEmpty) {
          setState(() {
            _renderer.srcObject = event.streams[0];
            _videoConnected = true;
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
                _videoConnected = true;
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
      _pc = await createPeerConnection({
        'iceServers': [
          {'urls': 'stun:stun.l.google.com:19302'}
        ],
      });
      _pc!.onTrack = (event) {
        if (_disposed || !mounted) return;
        if (event.streams.isNotEmpty) {
          setState(() {
            _renderer.srcObject = event.streams[0];
            _videoConnected = true;
          });
        }
      };
      _pc!.onConnectionState = _handleConnectionState;
      final offer = await _pc!.createOffer(
          {'offerToReceiveVideo': true, 'offerToReceiveAudio': false});
      await _pc!.setLocalDescription(offer);
      final sdp = await ApiService.sendOffer(offer.sdp!, mode: 'admin');
      if (sdp != null && !_disposed) {
        await _pc!.setRemoteDescription(RTCSessionDescription(sdp, 'answer'));
      }
    } catch (_) {
      if (mounted) setState(() => _videoConnected = false);
    }
  }

  // ── Timers ────────────────────────────────────────────

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
    setState(() {
      _pendingServer = true;
      _scanError = null;
    });
    _serverTimer = Timer(const Duration(seconds: 10), () {
      if (!mounted || !_pendingServer) return;
      setState(() {
        _pendingServer = false;
        _scanError = 'No response from server. Check your connection.';
      });
    });
  }

  void _serverResponded() {
    _serverTimer?.cancel();
    _pendingServer = false;
  }

  // ── Socket callbacks ──────────────────────────────────

  void _registerCallbacks() {
    _socket.connect(
      onAdminSessionOpened: (data) {
        if (!mounted) return;
        _openingTimer?.cancel();
        _serverResponded();
        _sessionId = data['session_id'];
        final List raw = data['mismatches'] ?? [];
        _mismatchMap = {
          for (final m in raw) m['pid'].toString(): m['expected_lid'] as int
        };
        _remaining = List<String>.from(_mismatchMap.keys);
        _cardAnim.forward(from: 0);
        _autoSelect();
      },
      onAdminSessionError: (data) {
        if (!mounted) return;
        _openingTimer?.cancel();
        _serverResponded();
        final msg = data['message'] ?? 'session_error';

        // 'no_active_mismatches' means the alarm briefly cleared between the
        // alarm_triggered event and the session start (the alarm fires per-slot
        // so it can resolve and re-trigger within ~200 ms).  Retry once after
        // a short delay — by then the re-fired alarm will have registered its
        // mismatches on the server.
        if (msg == 'no_active_mismatches') {
          Future.delayed(const Duration(milliseconds: 600), () {
            if (!mounted || _disposed || _step != _Step.opening) return;
            _startOpeningTimer();
            _socket.adminSessionStart(widget.password);
          });
          return;
        }

        setState(() {
          _step = _Step.error;
          _errorText = data['message'] ?? 'Session error';
        });
      },
      onAdminRemoveOk: (_) {
        if (!mounted) return;
        _serverResponded();
        setState(() {
          _step = _Step.awaitingScan;
          _scanError = null;
        });
      },
      onAdminQrResult: (data) {
        if (!mounted) return;
        _serverResponded();
        final pid       = data['pid'].toString();
        final needsDep  = data['needs_deposit'] == true;
        final occupied  = data['target_occupied'] == true;
        final sameSlot  = data['same_slot'] == true;

        _currentPid  = pid;
        _sameSlot    = sameSlot;
        _expectedLid = data['expected_lid'] != null
            ? int.parse(data['expected_lid'].toString())
            : null;

        if (needsDep) {
          setState(() {
            _step = _Step.needsDeposit;
            _infoMessage = data['message'];
          });
        } else if (occupied) {
          setState(() => _step = _Step.stagingNeeded);
        } else {
          setState(() => _step = _Step.placing);
        }
      },
      onAdminNoQrResult: (_) {
        if (!mounted) return;
        _serverResponded();
        setState(() {
          _step = _Step.noQrHandled;
          _currentPid = null;
        });
      },
      onAdminStageOk: (data) {
        if (!mounted) return;
        _serverResponded();
        _staged.add(data['pid'].toString());
        _currentPid = null;
        _autoSelect();
      },
      onAdminUnstageOk: (data) {
        if (!mounted) return;
        _serverResponded();
        final pid = data['pid'].toString();
        _staged.remove(pid);
        _currentPid  = pid;
        _sameSlot    = false;
        _expectedLid = _mismatchMap[pid];
        setState(() => _step = _Step.placing);
      },
      onAdminPlaceResult: (data) {
        if (!mounted) return;
        _serverResponded();
        if (_currentPid != null) _remaining.remove(_currentPid);
        _currentPid = null;
        _sameSlot   = false;
        _remaining  = List<String>.from(data['remaining'] ?? []);
        _staged     = List<String>.from(data['staged']    ?? []);
        if (_staged.isNotEmpty) {
          setState(() => _step = _Step.unstageNext);
        } else if (_remaining.isEmpty) {
          _socket.adminSessionClose();
        } else {
          _autoSelect();
        }
      },
      onAdminMissingResult: (data) {
        if (!mounted) return;
        _serverResponded();
        _remaining.remove(data['pid'].toString());
        if (_staged.isNotEmpty) {
          setState(() => _step = _Step.unstageNext);
        } else if (_remaining.isEmpty) {
          _socket.adminSessionClose();
        } else {
          _autoSelect();
        }
      },
      onAdminSessionClosed: (data) {
        if (!mounted) return;
        _serverResponded();
        _sessionCompleted = true;
        _summary      = data['summary'];
        _evidenceKept = data['evidence_kept'] == true;
        setState(() => _step = _Step.sessionDone);
        _cardAnim.forward(from: 0);
      },
      onAdminOperationError: (data) {
        if (!mounted) return;
        _serverResponded();
        final msg = data['message']?.toString() ?? 'Unknown error';
        if (_step == _Step.scanning) {
          setState(() {
            _step = _Step.awaitingScan;
            _scanError = _friendlyError(msg);
          });
          return;
        }
        setState(() => _scanError = _friendlyError(msg));
      },
    );
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

  // ── Actions ───────────────────────────────────────────

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
    if (_staged.isNotEmpty) {
      setState(() => _step = _Step.unstageNext);
      return;
    }
    if (_remaining.isNotEmpty) {
      _selectPhone(_remaining.first);
      return;
    }
    _socket.adminSessionClose();
  }

  void _selectPhone(String pid) {
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
    _waitForServer();
    _socket.adminRemovePhone(_currentLid!);
  }

  void _onScanQr() {
    _waitForServer();
    setState(() { _step = _Step.scanning; _scanError = null; });
    _socket.adminQrScanned();
  }

  void _onNoQrCode() {
    _waitForServer();
    setState(() { _step = _Step.scanning; _scanError = null; });
    _socket.adminNoQrFound();
  }

  void _onPlaced() {
    if (_expectedLid == null) {
      setState(() => _scanError =
      'Cannot place: expected slot is unknown. '
          'Tap "Handle a different phone" and try again.');
      return;
    }
    _waitForServer();
    _socket.adminPlacePhone(_expectedLid!);
  }

  void _onStaged()     { _waitForServer(); _socket.adminStagePhone(); }

  void _onUnstageNext() {
    if (_staged.isEmpty) return;
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

  // ── Lifecycle ─────────────────────────────────────────

  @override
  void dispose() {
    _disposed = true;
    _openingTimer?.cancel();
    _serverTimer?.cancel();
    _cardAnim.dispose();
    _socket.clearAdminCallbacks();
    _pc?.onTrack = null;
    _pc?.onConnectionState = null;
    _pc?.close();
    _renderer.srcObject = null;
    _renderer.dispose();
    ApiService.cancelAdmin();
    super.dispose();
  }

  // ── Build ─────────────────────────────────────────────
  //
  // Layout: Column (fills the scaffold)
  //   ├── Expanded → video (fills all remaining space above the card)
  //   └── _buildCard() → SlideTransition card (intrinsic height)
  //
  // The top bar overlays the video via a Stack inside the Expanded.
  // The card sits below the video — never overlaps or hides footage.

  @override
  Widget build(BuildContext context) {
    final canPop = _step == _Step.sessionDone || _step == _Step.error;
    return PopScope(
      canPop: canPop,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop) {
          ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
            content: Text('Use the button below to close the session properly.'),
            duration: Duration(seconds: 2),
          ));
        }
      },
      child: Scaffold(
        backgroundColor: Colors.black,
        body: SafeArea(
          child: Column(
            children: [
              // ── Video + top bar overlay — fills remaining space ──
              Expanded(
                child: Stack(
                  fit: StackFit.expand,
                  children: [
                    _buildCamera(),
                    _buildTopBar(),
                  ],
                ),
              ),

              // ── Slide-up card — intrinsic height, never overlaps video ──
              SlideTransition(
                position: _cardSlide,
                child: _buildCard(),
              ),
            ],
          ),
        ),
      ),
    );
  }

  // ── Camera ────────────────────────────────────────────

  Widget _buildCamera() {
    return _videoConnected && _renderer.srcObject != null
        ? RTCVideoView(_renderer,
        objectFit: RTCVideoViewObjectFit.RTCVideoViewObjectFitContain)
        : Container(
      color: Colors.black,
      child: Center(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          const SizedBox(
            width: 28, height: 28,
            child: CircularProgressIndicator(
                strokeWidth: 2.5, color: Colors.white38),
          ),
          const SizedBox(height: 12),
          const Text('Connecting top camera…',
              style: TextStyle(
                  color: Colors.white38, fontSize: 13)),
        ]),
      ),
    );
  }

  // ── Top bar (overlaid on video, gradient fade) ────────

  Widget _buildTopBar() {
    final total = _mismatchMap.length;
    final done  = total - _remaining.length;
    final pct   = total > 0 ? done / total : 0.0;

    return Positioned(
      top: 0, left: 0, right: 0,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
        decoration: const BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topCenter,
            end: Alignment.bottomCenter,
            colors: [Colors.black87, Colors.transparent],
          ),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(children: [
              if (_step == _Step.awaitingScan || _step == _Step.scanning)
                _topBadge(Icons.fiber_manual_record, 'REC', Colors.red),
              const SizedBox(width: 8),
              _topBadge(Icons.videocam_outlined, 'TOP CAM', Colors.white24),
              const Spacer(),
              if (total > 0)
                Text('$done / $total',
                    style: const TextStyle(
                        color: Colors.white70, fontSize: 13)),
            ]),
            if (total > 0) ...[
              const SizedBox(height: 6),
              ClipRRect(
                borderRadius: BorderRadius.circular(2),
                child: LinearProgressIndicator(
                  value: pct,
                  minHeight: 3,
                  backgroundColor: Colors.white24,
                  valueColor: AlwaysStoppedAnimation(
                      _remaining.isEmpty
                          ? Colors.green
                          : Colors.deepOrangeAccent),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _topBadge(IconData icon, String label, Color bg) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
    decoration: BoxDecoration(
        color: bg, borderRadius: BorderRadius.circular(5)),
    child: Row(mainAxisSize: MainAxisSize.min, children: [
      Icon(icon, color: Colors.white, size: 11),
      const SizedBox(width: 4),
      Text(label,
          style: const TextStyle(
              color: Colors.white,
              fontSize: 10,
              fontWeight: FontWeight.w600)),
    ]),
  );

  // ── Card ──────────────────────────────────────────────

  Widget _buildCard() {
    return Container(
      decoration: BoxDecoration(
        color: const Color(0xFF1C1C1E),
        borderRadius: const BorderRadius.vertical(top: Radius.circular(24)),
        boxShadow: [
          BoxShadow(
              color: Colors.black.withOpacity(0.6),
              blurRadius: 24,
              spreadRadius: 4)
        ],
      ),
      child: Column(mainAxisSize: MainAxisSize.min, children: [
        const SizedBox(height: 10),
        Center(
          child: Container(
            width: 36, height: 4,
            decoration: BoxDecoration(
                color: Colors.white24,
                borderRadius: BorderRadius.circular(2)),
          ),
        ),
        const SizedBox(height: 4),
        // SingleChildScrollView so card content never overflows
        // on small screens or when the keyboard is up.
        SingleChildScrollView(
          padding: EdgeInsets.fromLTRB(
              20, 12, 20, MediaQuery.of(context).viewInsets.bottom + 24),
          child: _buildCardContent(),
        ),
      ]),
    );
  }

  // ── Card content ──────────────────────────────────────

  Widget _buildCardContent() {
    switch (_step) {
      case _Step.opening:
        return _openingContent();
      case _Step.selectingPhone:
        return _phoneSelectContent();
      case _Step.pickingUp:
        return _stepContent(
          icon: Icons.pan_tool_alt_outlined,
          color: Colors.orange,
          title: 'Pick up the object from ${_slotLabel(_currentLid)}',
          subtitle: 'Once you have it in hand, tap the button below.',
          error: _scanError,
          actions: [
            _primaryBtn("I've picked it up", Colors.orange, _onPickedUp),
            _changePhoneBtn(),
            const SizedBox(height: 4),
            Opacity(
              opacity: (_canDeclareCurrentMissing && !_pendingServer) ? 1.0 : 0.35,
              child: TextButton.icon(
                icon: const Icon(Icons.search_off,
                    size: 16, color: Colors.redAccent),
                label: Text(
                  _canDeclareCurrentMissing
                      ? "Can't find it — declare missing"
                      : "Can't find it (check all other phones first)",
                  style: const TextStyle(
                      color: Colors.redAccent, fontSize: 13),
                ),
                onPressed: (_canDeclareCurrentMissing && !_pendingServer)
                    ? _onDeclareCurrentMissing
                    : null,
              ),
            ),
          ],
        );
      case _Step.awaitingScan:
        return _stepContent(
          icon: Icons.qr_code_scanner,
          color: Colors.blueAccent,
          title: 'Hold the QR code under the camera',
          subtitle: 'Keep it still and fully visible.',
          error: _scanError,
          actions: [
            _primaryBtn('Scan QR Code', Colors.blueAccent, _onScanQr),
            const SizedBox(height: 8),
            OutlinedButton.icon(
              icon: const Icon(Icons.hide_image_outlined, size: 18),
              label: const Text('No QR Code on this object'),
              style: OutlinedButton.styleFrom(
                  minimumSize: const Size(double.infinity, 48),
                  side: const BorderSide(color: Colors.white30)),
              onPressed: _pendingServer ? null : _onNoQrCode,
            ),
            _changePhoneBtn(),
          ],
        );
      case _Step.scanning:
        return _stepContent(
          icon: Icons.radar,
          color: Colors.blueAccent,
          title: 'Scanning…',
          subtitle: 'Keep the QR code still — up to 15 seconds.',
          loading: true,
          actions: const [],
        );
      case _Step.placing:
        final title = _sameSlot
            ? 'Place it back in ${_slotLabel(_expectedLid)}'
            : 'Place ${_displayPid(_currentPid)} in ${_slotLabel(_expectedLid)}';
        final subtitle = _sameSlot
            ? 'This phone belongs here. Slide it back in and tap Done.'
            : "Slide it in without covering it. Tap Done when it's in.";
        final btnLabel = _sameSlot
            ? 'Done — back in ${_slotLabel(_expectedLid)}'
            : 'Done — placed in ${_slotLabel(_expectedLid)}';
        return _stepContent(
          icon: _sameSlot ? Icons.undo_outlined : Icons.download_done_outlined,
          color: Colors.greenAccent,
          title: title,
          subtitle: subtitle,
          error: _scanError,
          actions: [_primaryBtn(btnLabel, Colors.green, _onPlaced)],
        );
      case _Step.stagingNeeded:
        return _stepContent(
          icon: Icons.table_restaurant_outlined,
          color: Colors.amber,
          title: 'Target ${_slotLabel(_expectedLid)} is occupied — stage this phone',
          subtitle:
          'Place ${_displayPid(_currentPid)} in a staging zone '
              '(marked area on the lid), then handle the blocking phone.',
          error: _scanError,
          actions: [
            _primaryBtn("I've placed it in staging", Colors.amber, _onStaged),
          ],
        );
      case _Step.unstageNext:
        final pid = _staged.isNotEmpty ? _staged.first : '?';
        final lid = _mismatchMap[pid] ?? 0;
        return _stepContent(
          icon: Icons.unarchive_outlined,
          color: Colors.tealAccent,
          title: 'Retrieve ${_displayPid(pid)} from staging',
          subtitle: 'Pick it up from the staging zone, '
              'then place it in slot ${lid + 1}.',
          error: _scanError,
          actions: [
            _primaryBtn("I've retrieved it", Colors.teal, _onUnstageNext),
          ],
        );
      case _Step.noQrHandled:
        return _stepContent(
          icon: Icons.no_photography_outlined,
          color: Colors.grey,
          title: 'Unidentified object removed',
          subtitle: 'Slot cleared. Evidence recorded permanently.',
          error: _scanError,
          actions: [_primaryBtn(_nextLabel(), null, _onNextAfterInfo)],
        );
      case _Step.needsDeposit:
        return _stepContent(
          icon: Icons.info_outline,
          color: Colors.indigoAccent,
          title: 'Phone needs normal deposit',
          subtitle: _infoMessage ?? 'This phone has no storage record.',
          error: _scanError,
          actions: [_primaryBtn(_nextLabel(), null, _onNextAfterInfo)],
        );
      case _Step.declaringMissing:
        return _stepContent(
          icon: Icons.hourglass_top,
          color: Colors.grey,
          title: 'Waiting for confirmation…',
          subtitle: 'Confirm the declaration in the dialog above.',
          loading: true,
          actions: const [],
        );
      case _Step.sessionDone:
        return _doneContent();
      case _Step.error:
        return _errorContent();
    }
  }

  // ── Reusable sub-widgets ──────────────────────────────

  Widget _openingContent() {
    if (_openingTimedOut) {
      return Column(mainAxisSize: MainAxisSize.min, children: [
        const Icon(Icons.wifi_off, color: Colors.orange, size: 40),
        const SizedBox(height: 12),
        const Text('No response from server',
            style: TextStyle(
                fontSize: 17, fontWeight: FontWeight.bold, color: Colors.white)),
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
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('Cancel',
                style: TextStyle(color: Colors.white54))),
      ]);
    }
    return Column(mainAxisSize: MainAxisSize.min, children: [
      const SizedBox(height: 8),
      const SizedBox(
          width: 28, height: 28,
          child: CircularProgressIndicator(
              strokeWidth: 2.5, color: Colors.white54)),
      const SizedBox(height: 16),
      const Text('Opening session…',
          style: TextStyle(
              fontSize: 17, fontWeight: FontWeight.bold, color: Colors.white)),
      const SizedBox(height: 6),
      const Text('Authenticating and loading mismatch list.',
          style: TextStyle(color: Colors.white54, fontSize: 13)),
      const SizedBox(height: 8),
    ]);
  }

  Widget _phoneSelectContent() {
    final hasStaged = _staged.isNotEmpty;
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(children: [
          const Icon(Icons.swap_horiz,
              color: Colors.deepOrangeAccent, size: 20),
          const SizedBox(width: 8),
          const Expanded(
            child: Text('Choose which phone to handle',
                style: TextStyle(
                    fontSize: 16,
                    fontWeight: FontWeight.bold,
                    color: Colors.white)),
          ),
          TextButton(
            style: TextButton.styleFrom(
                padding: EdgeInsets.zero, minimumSize: Size.zero),
            onPressed: _autoSelect,
            child: const Text('Auto',
                style: TextStyle(
                    color: Colors.deepOrangeAccent,
                    fontSize: 13,
                    fontWeight: FontWeight.w600)),
          ),
        ]),
        const SizedBox(height: 4),
        Text(
          hasStaged
              ? 'Retrieve staged phones before picking new ones.'
              : 'Tap any phone to go straight to pick-up.',
          style: const TextStyle(color: Colors.white54, fontSize: 13),
        ),
        const SizedBox(height: 14),
        if (hasStaged) ...[
          const Text('STAGED',
              style: TextStyle(
                  color: Colors.tealAccent,
                  fontSize: 11,
                  fontWeight: FontWeight.w700,
                  letterSpacing: 1.2)),
          const SizedBox(height: 6),
          ..._staged.map((pid) => _phoneChip(
            pid: pid,
            lid: _mismatchMap[pid] ?? 0,
            color: Colors.teal,
            icon: Icons.unarchive_outlined,
            onTap: () => _socket.adminUnstagePhone(pid),
          )),
          const SizedBox(height: 12),
        ],
        if (_remaining.isNotEmpty) ...[
          if (hasStaged)
            const Text('REMAINING',
                style: TextStyle(
                    color: Colors.white38,
                    fontSize: 11,
                    fontWeight: FontWeight.w700,
                    letterSpacing: 1.2)),
          if (hasStaged) const SizedBox(height: 6),
          ..._remaining.map((pid) {
            final isDefault = pid == _remaining.first && !hasStaged;
            return _phoneChip(
              pid: pid,
              lid: _mismatchMap[pid] ?? 0,
              color: isDefault ? Colors.deepOrangeAccent : Colors.white54,
              icon: isDefault ? Icons.smartphone : Icons.smartphone_outlined,
              onTap: () => _selectPhone(pid),
            );
          }),
        ],
        const SizedBox(height: 8),
      ],
    );
  }

  Widget _phoneChip({
    required String pid,
    required int lid,
    required Color color,
    required IconData icon,
    required VoidCallback onTap,
  }) =>
      Padding(
        padding: const EdgeInsets.only(bottom: 8),
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(12),
          child: Container(
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
            decoration: BoxDecoration(
              color: color.withOpacity(0.12),
              border: Border.all(color: color.withOpacity(0.35)),
              borderRadius: BorderRadius.circular(12),
            ),
            child: Row(children: [
              Icon(icon, color: color, size: 20),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(_displayPid(pid),
                        style: TextStyle(
                            color: color,
                            fontSize: 14,
                            fontWeight: FontWeight.w600)),
                    Text('Expected in slot ${lid + 1}',
                        style: const TextStyle(
                            color: Colors.white54, fontSize: 12)),
                  ],
                ),
              ),
              Icon(Icons.chevron_right,
                  color: color.withOpacity(0.6), size: 20),
            ]),
          ),
        ),
      );

  Widget _stepContent({
    required IconData icon,
    required Color color,
    required String title,
    required String subtitle,
    String? error,
    bool loading = false,
    List<Widget> actions = const [],
  }) =>
      Column(mainAxisSize: MainAxisSize.min, children: [
        if (loading)
          SizedBox(
              width: 36, height: 36,
              child: CircularProgressIndicator(strokeWidth: 2.5, color: color))
        else
          Icon(icon, color: color, size: 38),
        const SizedBox(height: 14),
        Text(title,
            textAlign: TextAlign.center,
            style: const TextStyle(
                fontSize: 17, fontWeight: FontWeight.bold, color: Colors.white)),
        const SizedBox(height: 8),
        Text(subtitle,
            textAlign: TextAlign.center,
            style: const TextStyle(
                color: Colors.white54, fontSize: 13, height: 1.45)),
        if (error != null) ...[
          const SizedBox(height: 12),
          Container(
            width: double.infinity,
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
            decoration: BoxDecoration(
              color: Colors.red.withOpacity(0.15),
              border: Border.all(color: Colors.red.withOpacity(0.4)),
              borderRadius: BorderRadius.circular(10),
            ),
            child: Row(children: [
              const Icon(Icons.error_outline, color: Colors.redAccent, size: 18),
              const SizedBox(width: 10),
              Expanded(
                  child: Text(error,
                      style: const TextStyle(
                          color: Colors.redAccent, fontSize: 13))),
            ]),
          ),
        ],
        const SizedBox(height: 20),
        ...actions,
      ]);

  Widget _doneContent() {
    final resolved   = (_summary?['resolved']         as List?)?.length ?? 0;
    final missing    = (_summary?['declared_missing'] as List?)?.length ?? 0;
    final depositing = (_summary?['needs_deposit']    as List?)?.length ?? 0;
    return Column(mainAxisSize: MainAxisSize.min, children: [
      const Icon(Icons.check_circle_outline,
          color: Colors.greenAccent, size: 48),
      const SizedBox(height: 14),
      const Text('All done! Have a nice day 🎉',
          textAlign: TextAlign.center,
          style: TextStyle(
              fontSize: 20, fontWeight: FontWeight.bold, color: Colors.white)),
      const SizedBox(height: 14),
      Wrap(
          spacing: 8, runSpacing: 6, alignment: WrapAlignment.center,
          children: [
            _summaryChip('$resolved resolved', Colors.green),
            if (missing > 0) _summaryChip('$missing missing', Colors.red),
            if (depositing > 0)
              _summaryChip('$depositing to deposit', Colors.indigo),
          ]),
      if (_evidenceKept) ...[
        const SizedBox(height: 12),
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
          decoration: BoxDecoration(
            color: Colors.orange.withOpacity(0.15),
            borderRadius: BorderRadius.circular(10),
            border: Border.all(color: Colors.orange.withOpacity(0.4)),
          ),
          child: const Row(children: [
            Icon(Icons.warning_amber_outlined, color: Colors.orange, size: 18),
            SizedBox(width: 10),
            Expanded(
                child: Text('Evidence kept — anomalies were recorded.',
                    style: TextStyle(color: Colors.orange, fontSize: 13))),
          ]),
        ),
      ],
      const SizedBox(height: 24),
      _primaryBtn('Close', Colors.green,
              () => Navigator.of(context).pop(true)),
    ]);
  }

  Widget _summaryChip(String label, Color color) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 5),
    decoration: BoxDecoration(
        color: color.withOpacity(0.15),
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: color.withOpacity(0.4))),
    child: Text(label,
        style: TextStyle(
            color: color, fontSize: 13, fontWeight: FontWeight.w600)),
  );

  Widget _errorContent() => Column(mainAxisSize: MainAxisSize.min, children: [
    const Icon(Icons.error_outline, color: Colors.redAccent, size: 44),
    const SizedBox(height: 14),
    const Text('Session error',
        style: TextStyle(
            fontSize: 17, fontWeight: FontWeight.bold, color: Colors.white)),
    const SizedBox(height: 8),
    Text(_errorText ?? 'Something went wrong.',
        textAlign: TextAlign.center,
        style: const TextStyle(color: Colors.white54, fontSize: 13)),
    const SizedBox(height: 24),
    _primaryBtn('Close', Colors.grey,
            () => Navigator.of(context).pop(false)),
  ]);

  Widget _primaryBtn(String label, Color? color, VoidCallback onPressed) =>
      ElevatedButton(
        style: ElevatedButton.styleFrom(
          backgroundColor: color ?? Colors.deepOrangeAccent,
          minimumSize: const Size(double.infinity, 50),
          shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(12)),
          textStyle:
          const TextStyle(fontSize: 15, fontWeight: FontWeight.w600),
        ),
        onPressed: _pendingServer ? null : onPressed,
        child: _pendingServer
            ? const SizedBox(
            width: 20, height: 20,
            child: CircularProgressIndicator(
                strokeWidth: 2.5, color: Colors.white70))
            : Text(label),
      );

  Widget _changePhoneBtn() => Padding(
    padding: const EdgeInsets.only(top: 4),
    child: TextButton.icon(
      icon: const Icon(Icons.swap_horiz, size: 16, color: Colors.white38),
      label: const Text('Handle a different phone first',
          style: TextStyle(color: Colors.white38, fontSize: 13)),
      onPressed: _pendingServer
          ? null
          : () => setState(() {
        _currentPid  = null;
        _currentLid  = null;
        _sameSlot    = false;
        _scanError   = null;
        _step        = _Step.selectingPhone;
      }),
    ),
  );

  String _nextLabel() =>
      (_remaining.isEmpty && _staged.isEmpty) ? 'Finish Session' : 'Next';
}