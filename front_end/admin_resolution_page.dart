import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'socket_service.dart';
import 'api_service.dart';

enum _Step {
  opening,
  selectingPhone,
  pickingUp,          // admin picks object from slot; QR scan starts automatically
  scanning,           // QR scan running; "No QR" button visible after delay
  trackingAdmin,      // PhoneTracker running; phone moving to dest / staging
  autoStaged,         // tracker detected phone placed in staging zone
  unstageNext,        // prompt admin to retrieve staged phone (tracking starts auto)
  noQrHandled,        // foreign object handled
  needsDeposit,       // phone has no DB record
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
  bool _qrVisible = true;    // updated by tracking_update during trackingAdmin

  Map<String, dynamic>? _summary;
  bool _evidenceKept = false;
  bool _forceClosed  = false;
  bool _sessionCompleted = false;

  Timer? _openingTimer;
  bool _openingTimedOut = false;

  bool _pendingServer = false;
  Timer? _serverTimer;

  // True only if the server confirmed the session opened.
  bool _sessionWasOpened = false;

  // "No QR" button appears after this delay once scanning starts
  bool _noQrButtonVisible = false;
  Timer? _noQrButtonTimer;

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
    // 30 s — QR scan itself can take up to QR_SCAN_TIMEOUT (90 s on server),
    // but this timer only covers the initial round-trip before QR scanning starts.
    _serverTimer = Timer(const Duration(seconds: 30), () {
      if (!mounted || !_pendingServer) return;
      setState(() {
        _pendingServer = false;
        _scanErimport 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'socket_service.dart';
import 'api_service.dart';
import 'scan_success_page.dart'; // DVWBottomSheet

enum _Step {
  opening,
  selectingPhone,
  pickingUp,
  scanning,
  trackingAdmin,
  autoStaged,
  unstageNext,
  needsDeposit,   // phone found but no DB record — show deposit button
  depositInProgress, // DVW deposit sheet is open
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
  bool _qrVisible = true;

  // For the needs_deposit case: the PID found by QR scan
  String? _needsDepositPid;

  Map<String, dynamic>? _summary;
  bool _evidenceKept = false;
  bool _forceClosed  = false;
  bool _sessionCompleted = false;

  Timer? _openingTimer;
  bool _openingTimedOut = false;

  bool _pendingServer = false;
  Timer? _serverTimer;

  bool _sessionWasOpened = false;

  bool _noQrButtonVisible = false;
  Timer? _noQrButtonTimer;

  late AnimationController _cardAnim;
  late Animation<Offset> _cardSlide;

  final RTCVideoRenderer _renderer = RTCVideoRenderer();
  RTCPeerConnection? _pc;
  bool _videoConnected = false;
  bool _disposed = false;
  bool _isReconnecting = false;

  // ── Display helpers ───────────────────────────────────

  static String _slotLabel(int? lid, {int? row, int? col}) {
    final n = (lid ?? 0) + 1;
    if (row != null && col != null) return 'slot $n (row $row, col $col)';
    return 'slot $n';
  }

  static String _displayPid(String? pid) {
    if (pid == null) return '?';
    if (pid.startsWith('unknown-')) return 'Unidentified object';
    if (pid.length > 18) return '...${pid.substring(pid.length - 12)}';
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
    _serverTimer = Timer(const Duration(seconds: 30), () {
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
        _sessionWasOpened = true;
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
        _noQrButtonVisible = false;
        _noQrButtonTimer?.cancel();
        _noQrButtonTimer = Timer(const Duration(seconds: 8), () {
          if (!mounted || _step != _Step.scanning) return;
          setState(() => _noQrButtonVisible = true);
        });
        setState(() {
          _step      = _Step.scanning;
          _scanError = null;
        });
      },
      onAdminQrResult: (data) {
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
          setState(() {
            _step           = _Step.needsDeposit;
            _needsDepositPid = pid;
            _infoMessage    = data['message'];
          });
        }
        // If auto_tracking == true, tracking_started will arrive shortly.
      },
      onAdminNoQrResult: (_) {
        if (!mounted) return;
        _serverResponded();
        _noQrButtonTimer?.cancel();
        setState(() {
          _step       = _Step.noQrHandled;
          _currentPid = null;
        });
      },
      onAdminAutoStaged: (data) {
        if (!mounted) return;
        _serverResponded();
        final pid         = data['pid'].toString();
        final destLid     = (data['dest_lid'] as num?)?.toInt();
        final blockingPid = data['blocking_pid']?.toString();
        _staged.add(pid);
        _remaining = List<String>.from(data['remaining'] ?? _remaining);
        setState(() {
          _step      = _Step.autoStaged;
          _scanError = null;
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
            if (byLid.isNotEmpty) _selectPhone(byLid);
            else _autoSelect();
          } else {
            _autoSelect();
          }
        });
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
        _noQrButtonTimer?.cancel();
        _sessionCompleted = true;
        _summary        = data['summary'];
        _evidenceKept   = data['evidence_kept'] == true;
        _forceClosed    = data['force_closed'] == true;
        setState(() => _step = _Step.sessionDone);
        _cardAnim.forward(from: 0);
      },
      onAdminOperationError: (data) {
        if (!mounted) return;
        _serverResponded();
        final msg = data['message']?.toString() ?? 'Unknown error';
        if (_step == _Step.trackingAdmin) {
          setState(() {
            _step      = _Step.pickingUp;
            _scanError = _trackingFailureMsg(data['reason']?.toString() ?? msg);
          });
          return;
        }
        if (_step == _Step.scanning) {
          setState(() {
            _noQrButtonVisible = true;
            _scanError = _friendlyError(msg);
          });
          return;
        }
        setState(() => _scanError = _friendlyError(msg));
      },
      onTrackingStarted: (data) {
        if (!mounted) return;
        setState(() {
          _step      = _Step.trackingAdmin;
          _qrVisible = true;
          _scanError = null;
        });
      },
      onTrackingUpdate: (data) {
        if (!mounted) return;
        setState(() => _qrVisible = data['qr_visible'] as bool? ?? true);
      },
      onTrackingFailed: (data) {
        if (!mounted) return;
        final reason = data['reason'] as String? ?? '';
        setState(() {
          _step      = _Step.pickingUp;
          _scanError = _trackingFailureMsg(reason);
        });
      },
    );
  }

  String _trackingFailureMsg(String reason) {
    const map = {
      'qr_lost':      'QR code disappeared before the phone reached the slot. Retry.',
      'out_of_frame': 'Phone left the camera view. Move directly toward the slot and retry.',
      'timeout':      'Placement timed out. Retry.',
      'detect_timeout': 'Phone not detected. Make sure it enters the camera view and retry.',
      'phone_not_in_slot':    'Phone not detected in slot by internal camera. Retry.',
      'insertion_timeout':    'Phone did not complete insertion in time. Retry.',
      'stabilization_timeout':'Phone did not settle in time. Hold flat and retry.',
      'tracker_lost':         'Tracking lost before phone reached slot. Move steadily.',
      'error':                'Tracker error. Retry.',
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

  void _onNoQrCode() {
    _noQrButtonTimer?.cancel();
    _waitForServer();
    _socket.adminNoQrFound();
  }

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

  /// Launch a normal deposit for a phone found in the box with no DB record.
  /// Opens the DVWBottomSheet just like the student flow does.
  void _startDepositForNeedsDeposit() {
    final pid = _needsDepositPid;
    if (pid == null) return;

    setState(() => _step = _Step.depositInProgress);

    // Emit deposit socket event first, then show the sheet.
    _socket.deposit(pid);

    showModalBottomSheet(
      context: context,
      isDismissible: false,
      enableDrag: false,
      isScrollControlled: true,
      backgroundColor: Colors.transparent,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (_) => DVWBottomSheet(
        pid: pid,
        isDeposit: true,
        socketService: _socket,
        onComplete: () {
          // Deposit complete — remove this pid from remaining (if present)
          // and continue the session.
          if (mounted) {
            _remaining.remove(pid);
            _needsDepositPid = null;
            _onNextAfterInfo();
          }
        },
      ),
    ).then((_) {
      // Sheet was dismissed (cancel or error) — go back to selectingPhone
      if (mounted && _step == _Step.depositInProgress) {
        setState(() {
          _needsDepositPid = null;
          _step = _Step.selectingPhone;
        });
        _autoSelect();
      }
    });
  }

  // ── Lifecycle ─────────────────────────────────────────

  @override
  void dispose() {
    _disposed = true;
    _openingTimer?.cancel();
    _serverTimer?.cancel();
    _noQrButtonTimer?.cancel();
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
              Expanded(
                child: Stack(
                  fit: StackFit.expand,
                  children: [
                    _buildCamera(),
                    _buildTopBar(),
                  ],
                ),
              ),
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
          const Text('Connecting top camera...',
              style: TextStyle(
                  color: Colors.white38, fontSize: 13)),
        ]),
      ),
    );
  }

  Widget _buildTopBar() {
    final total = _mismatchMap.length;
    final done  = total - _remaining.length;
    return _TopProgressBar(
      total: total,
      done: done,
      isRecording: _step == _Step.scanning || _step == _Step.trackingAdmin,
    );
  }

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
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: Row(
            children: [
              const Spacer(),
              Container(
                width: 36, height: 4,
                decoration: BoxDecoration(
                    color: Colors.white24,
                    borderRadius: BorderRadius.circular(2)),
              ),
              const Spacer(),
              if (_step != _Step.opening &&
                  _step != _Step.sessionDone &&
                  _step != _Step.error &&
                  _step != _Step.depositInProgress)
                _forceCloseButton(),
            ],
          ),
        ),
        const SizedBox(height: 4),
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
          subtitle: 'Once you have it in hand, tap the button. '
              'QR scan will start automatically.',
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
                      ? "Can't find it - declare missing"
                      : "Can't find it (check all other phones first)",
                  style: const TextStyle(color: Colors.redAccent, fontSize: 13),
                ),
                onPressed: (_canDeclareCurrentMissing && !_pendingServer)
                    ? _onDeclareCurrentMissing
                    : null,
              ),
            ),
          ],
        );
      case _Step.scanning:
        return _stepContent(
          icon: Icons.qr_code_scanner,
          color: Colors.blueAccent,
          title: 'Scanning for QR code...',
          subtitle: 'Hold the QR code under the camera and keep it still. '
              'Tracking will start as soon as it is found.',
          loading: !_noQrButtonVisible,
          error: _scanError,
          actions: [
            if (_noQrButtonVisible) ...[
              OutlinedButton.icon(
                icon: const Icon(Icons.hide_image_outlined, size: 18),
                label: const Text('No QR code on this object'),
                style: OutlinedButton.styleFrom(
                    minimumSize: const Size(double.infinity, 48),
                    side: const BorderSide(color: Colors.white30)),
                onPressed: _pendingServer ? null : _onNoQrCode,
              ),
              _changePhoneBtn(),
            ],
          ],
        );
      case _Step.trackingAdmin:
        return _stepContent(
          icon: Icons.my_location_outlined,
          color: Colors.greenAccent,
          title: 'Move phone to ${_slotLabel(_expectedLid)}',
          subtitle: 'Move it directly to the slot - or into a staging zone '
              'if the slot is occupied. The system detects both automatically.',
          error: _scanError,
          actions: [
            _TrackingQrBadge(qrVisible: _qrVisible),
          ],
        );
      case _Step.autoStaged:
        return _stepContent(
          icon: Icons.hourglass_top,
          color: Colors.amber,
          title: 'Phone staged - loading next step...',
          subtitle: 'The system detected the phone in the staging zone and is '
              'selecting the next phone to handle.',
          loading: true,
          actions: const [],
        );
      case _Step.unstageNext:
        final pid = _staged.isNotEmpty ? _staged.first : '?';
        final lid = _mismatchMap[pid] ?? 0;
        return _stepContent(
          icon: Icons.unarchive_outlined,
          color: Colors.tealAccent,
          title: 'Retrieve ${_displayPid(pid)} from staging',
          subtitle: 'Pick it up from the staging zone. '
              'Tracking to slot ${lid + 1} will start automatically.',
          error: _scanError,
          actions: [
            _primaryBtn("I've retrieved it from staging", Colors.teal, _onUnstageNext),
          ],
        );
      case _Step.needsDeposit:
        // Phone has no storage record — offer to deposit it now.
        return _stepContent(
          icon: Icons.add_box_outlined,
          color: Colors.indigoAccent,
          title: 'Phone needs a normal deposit',
          subtitle: _infoMessage ??
              'This phone is registered but has no active storage record. '
              'Deposit it now to register it in the system.',
          error: _scanError,
          actions: [
            _primaryBtn(
              'Deposit this phone now',
              Colors.indigoAccent,
              _startDepositForNeedsDeposit,
            ),
            const SizedBox(height: 8),
            _primaryBtn(
              'Skip - handle next mismatch',
              null,
              _onNextAfterInfo,
            ),
          ],
        );
      case _Step.depositInProgress:
        // DVWBottomSheet is showing on top; show a waiting state here.
        return _stepContent(
          icon: Icons.move_to_inbox_outlined,
          color: Colors.indigoAccent,
          title: 'Deposit in progress...',
          subtitle: 'Follow the instructions in the panel below to complete the deposit.',
          loading: true,
          actions: const [],
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
      case _Step.declaringMissing:
        return _stepContent(
          icon: Icons.hourglass_top,
          color: Colors.grey,
          title: 'Waiting for confirmation...',
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
            onPressed: () => Navigator.of(context).pop(
                _sessionWasOpened ? false : null),
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
      const Text('Opening session...',
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
    final unresolved = (_summary?['unresolved']       as List?)?.length ?? 0;

    final icon  = _forceClosed
        ? Icons.cancel_outlined
        : Icons.check_circle_outline;
    final iconColor = _forceClosed ? Colors.orange : Colors.greenAccent;
    final title = _forceClosed
        ? 'Session ended early'
        : 'All done! Have a nice day';

    return Column(mainAxisSize: MainAxisSize.min, children: [
      Icon(icon, color: iconColor, size: 48),
      const SizedBox(height: 14),
      Text(title,
          textAlign: TextAlign.center,
          style: const TextStyle(
              fontSize: 20, fontWeight: FontWeight.bold, color: Colors.white)),
      const SizedBox(height: 14),
      Wrap(
          spacing: 8, runSpacing: 6, alignment: WrapAlignment.center,
          children: [
            if (resolved   > 0) _summaryChip('$resolved resolved', Colors.green),
            if (missing    > 0) _summaryChip('$missing missing', Colors.red),
            if (depositing > 0) _summaryChip('$depositing to deposit', Colors.indigo),
            if (unresolved > 0) _summaryChip('$unresolved unresolved', Colors.orange),
          ]),
      if (_forceClosed) ...[
        const SizedBox(height: 12),
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
          decoration: BoxDecoration(
            color: Colors.orange.withOpacity(0.15),
            borderRadius: BorderRadius.circular(10),
            border: Border.all(color: Colors.orange.withOpacity(0.4)),
          ),
          child: const Row(children: [
            Icon(Icons.folder_open_outlined, color: Colors.orange, size: 18),
            SizedBox(width: 10),
            Expanded(
                child: Text(
                    'Evidence saved as NotFullyResolved. '
                    'Unresolved mismatches will re-alarm.',
                    style: TextStyle(color: Colors.orange, fontSize: 13))),
          ]),
        ),
      ] else if (_evidenceKept) ...[
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
                child: Text('Evidence kept - anomalies were recorded.',
                    style: TextStyle(color: Colors.orange, fontSize: 13))),
          ]),
        ),
      ],
      const SizedBox(height: 24),
      _primaryBtn('Close', _forceClosed ? Colors.deepOrangeAccent : Colors.green,
              () => Navigator.of(context).pop(_forceClosed ? false : true)),
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
            () => Navigator.of(context).pop(
                _sessionWasOpened ? false : null)),
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

  Widget _forceCloseButton() {
    return GestureDetector(
      onLongPress: () => _onForceClose(safe: false),
      child: TextButton.icon(
        icon: const Icon(Icons.close, size: 14, color: Colors.white38),
        label: const Text('End session',
            style: TextStyle(color: Colors.white38, fontSize: 12)),
        style: TextButton.styleFrom(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
          minimumSize: Size.zero,
          tapTargetSize: MaterialTapTargetSize.shrinkWrap,
        ),
        onPressed: _pendingServer ? null : () => _onForceClose(safe: true),
      ),
    );
  }

  Future<void> _onForceClose({required bool safe}) async {
    final hasPhoneInHand = _step == _Step.scanning || _step == _Step.trackingAdmin;
    final hasStaged      = _staged.isNotEmpty;
    final hasRemaining   = _remaining.isNotEmpty;

    String warningText;
    if (!safe) {
      warningText = 'DEBUG: Force-close regardless of state.\n'
          'All evidence will be saved as NotFullyResolved.';
    } else if (hasPhoneInHand) {
      warningText = 'You have a phone in hand. Put it down first, '
          'then close the session.';
      if (mounted) setState(() => _scanError = warningText);
      return;
    } else if (hasStaged) {
      warningText = 'There are staged phones. Resolve them first, '
          'then close the session.';
      if (mounted) setState(() => _scanError = warningText);
      return;
    } else {
      warningText = hasRemaining
          ? 'End session now?\n\n'
            '${_remaining.length} mismatch(es) will remain unresolved.\n'
            'Evidence will be saved as NotFullyResolved.'
          : 'End session early? All resolved mismatches will be saved.';
    }

    final confirm = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        backgroundColor: const Color(0xFF1C1C1E),
        title: Text(
          safe ? 'End session' : 'Force close (debug)',
          style: const TextStyle(color: Colors.white),
        ),
        content: Text(warningText,
            style: const TextStyle(color: Colors.white70, fontSize: 13)),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Cancel',
                style: TextStyle(color: Colors.white54)),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(
              backgroundColor: safe ? Colors.deepOrangeAccent : Colors.red,
            ),
            onPressed: () => Navigator.pop(context, true),
            child: Text(safe ? 'End session' : 'Force close'),
          ),
        ],
      ),
    );

    if (confirm != true || !mounted) return;
    _waitForServer();
    _socket.adminForceClose(safe: safe);
  }

  String _nextLabel() =>
      (_remaining.isEmpty && _staged.isEmpty) ? 'Finish Session' : 'Next';
}

// ── QR status badge ────────────────────────────────────────────────────────────

class _TrackingQrBadge extends StatelessWidget {
  final bool qrVisible;
  const _TrackingQrBadge({required this.qrVisible});

  @override
  Widget build(BuildContext context) {
    final color = qrVisible ? Colors.green : Colors.orange;
    final icon  = qrVisible ? Icons.qr_code_2 : Icons.qr_code_2_outlined;
    final label = qrVisible
        ? 'QR visible - move phone to destination slot'
        : 'QR not visible - keep QR facing up!';
    return AnimatedContainer(
      duration: const Duration(milliseconds: 300),
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
      decoration: BoxDecoration(
        color: color.withOpacity(0.12),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: color.withOpacity(0.4)),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        Icon(icon, color: color, size: 20),
        const SizedBox(width: 8),
        Flexible(
          child: Text(label,
              style: TextStyle(
                  color: color, fontSize: 13, fontWeight: FontWeight.w500)),
        ),
      ]),
    );
  }
}

// ── Session progress bar overlay ──────────────────────────────────────────────

class _TopProgressBar extends StatelessWidget {
  final int total;
  final int done;
  final bool isRecording;
  const _TopProgressBar({
    required this.total,
    required this.done,
    required this.isRecording,
  });

  static Widget _badge(IconData icon, String label, Color bg) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
    decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(5)),
    child: Row(mainAxisSize: MainAxisSize.min, children: [
      Icon(icon, color: Colors.white, size: 11),
      const SizedBox(width: 4),
      Text(label,
          style: const TextStyle(
              color: Colors.white, fontSize: 10, fontWeight: FontWeight.w600)),
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
            begin: Alignment.topCenter,
            end: Alignment.bottomCenter,
            colors: [Colors.black87, Colors.transparent],
          ),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
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
                  value: pct,
                  minHeight: 3,
                  backgroundColor: Colors.white24,
                  valueColor: AlwaysStoppedAnimation(
                      done >= total ? Colors.green : Colors.deepOrangeAccent),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}ror = 'No response from server. Check your connection.';
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
        _sessionWasOpened = true;
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
      // admin_remove_ok: QR scan is already running on server side.
      // Show scanning UI immediately (no button needed).
      onAdminRemoveOk: (_) {
        if (!mounted) return;
        _serverResponded();
        _noQrButtonVisible = false;
        _noQrButtonTimer?.cancel();
        // Show "No QR" button after 8 s to give the scanner time to find it
        _noQrButtonTimer = Timer(const Duration(seconds: 8), () {
          if (!mounted || _step != _Step.scanning) return;
          setState(() => _noQrButtonVisible = true);
        });
        setState(() {
          _step      = _Step.scanning;
          _scanError = null;
        });
      },
      // QR found: server already auto-started tracking.
      // Just update our state so the UI shows tracking.
      onAdminQrResult: (data) {
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
          setState(() {
            _step        = _Step.needsDeposit;
            _infoMessage = data['message'];
          });
        }
        // If auto_tracking == true the server already launched PhoneTracker.
        // tracking_started event will arrive shortly and move us to trackingAdmin.
      },
      onAdminNoQrResult: (_) {
        if (!mounted) return;
        _serverResponded();
        _noQrButtonTimer?.cancel();
        setState(() {
          _step       = _Step.noQrHandled;
          _currentPid = null;
        });
      },
      // admin_auto_staged: tracker detected phone placed in staging zone.
      // Server selected the next blocker automatically.
      onAdminAutoStaged: (data) {
        if (!mounted) return;
        _serverResponded();
        final pid         = data['pid'].toString();
        final destLid     = (data['dest_lid'] as num?)?.toInt();
        final blockingPid = data['blocking_pid']?.toString();
        _staged.add(pid);
        _remaining = List<String>.from(data['remaining'] ?? _remaining);
        setState(() {
          _step      = _Step.autoStaged;
          _scanError = null;
          _currentPid  = null;
          _currentLid  = destLid;
          _expectedLid = destLid;
        });
        // Auto-advance: tell the admin which slot to open next
        // by selecting the blocking phone immediately
        Future.delayed(const Duration(milliseconds: 400), () {
          if (!mounted) return;
          if (blockingPid != null && _mismatchMap.containsKey(blockingPid)) {
            _selectPhone(blockingPid);
          } else if (destLid != null) {
            // Unknown blocker — select by lid
            final byLid = _remaining.firstWhere(
                (p) => _mismatchMap[p] == destLid, orElse: () => '');
            if (byLid.isNotEmpty) _selectPhone(byLid);
            else _autoSelect();
          } else {
            _autoSelect();
          }
        });
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
        _noQrButtonTimer?.cancel();
        _sessionCompleted = true;
        _summary        = data['summary'];
        _evidenceKept   = data['evidence_kept'] == true;
        _forceClosed    = data['force_closed'] == true;
        setState(() => _step = _Step.sessionDone);
        _cardAnim.forward(from: 0);
      },
      onAdminOperationError: (data) {
        if (!mounted) return;
        _serverResponded();
        final msg = data['message']?.toString() ?? 'Unknown error';
        // Tracking failed: server reverted state, admin must pick up again
        if (_step == _Step.trackingAdmin) {
          setState(() {
            _step      = _Step.pickingUp;
            _scanError = _trackingFailureMsg(data['reason']?.toString() ?? msg);
          });
          return;
        }
        // QR scan error during scanning: show "No QR" button immediately
        if (_step == _Step.scanning) {
          setState(() {
            _noQrButtonVisible = true;
            _scanError = _friendlyError(msg);
          });
          return;
        }
        setState(() => _scanError = _friendlyError(msg));
      },
      onTrackingStarted: (data) {
        if (!mounted) return;
        setState(() {
          _step      = _Step.trackingAdmin;
          _qrVisible = true;
          _scanError = null;
        });
      },
      onTrackingUpdate: (data) {
        if (!mounted) return;
        setState(() => _qrVisible = data['qr_visible'] as bool? ?? true);
      },
      onTrackingFailed: (data) {
        if (!mounted) return;
        final reason = data['reason'] as String? ?? '';
        // Tracking failed mid-flight — let admin retry from pickingUp
        setState(() {
          _step      = _Step.pickingUp;
          _scanError = _trackingFailureMsg(reason);
        });
      },
    );
  }

  String _trackingFailureMsg(String reason) {
    const map = {
      'qr_lost':      'QR code disappeared before the phone reached the slot. Retry.',
      'out_of_frame': 'Phone left the camera view. Move directly toward the slot and retry.',
      'timeout':      'Placement timed out. Retry.',
      'detect_timeout': 'Phone not detected. Make sure it enters the camera view and retry.',
      'phone_not_in_slot':    'Phone not detected in slot by internal camera. Retry.',
      'insertion_timeout':    'Phone did not complete insertion in time. Retry.',
      'stabilization_timeout':'Phone did not settle in time. Hold flat and retry.',
      'tracker_lost':         'Tracking lost before phone reached slot. Move steadily.',
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
    // server auto-starts QR scan immediately after admin_remove_phone
    _socket.adminRemovePhone(_currentLid!);
  }

  void _onNoQrCode() {
    _noQrButtonTimer?.cancel();
    _waitForServer();
    _socket.adminNoQrFound();
  }

  void _onUnstageNext() {
    if (_staged.isEmpty) return;
    _waitForServer();
    // server auto-starts tracking after unstage
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
    _noQrButtonTimer?.cancel();
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
    return _TopProgressBar(
      total: total,
      done: done,
      isRecording: _step == _Step.scanning || _step == _Step.trackingAdmin,
    );
  }

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
        // ── Drag handle + force-close row ─────────────────
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: Row(
            children: [
              const Spacer(),
              Container(
                width: 36, height: 4,
                decoration: BoxDecoration(
                    color: Colors.white24,
                    borderRadius: BorderRadius.circular(2)),
              ),
              const Spacer(),
              // Force-close button — only shown during an active session
              if (_step != _Step.opening &&
                  _step != _Step.sessionDone &&
                  _step != _Step.error)
                _forceCloseButton(),
            ],
          ),
        ),
        const SizedBox(height: 4),
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
          subtitle: 'Once you have it in hand, tap the button. '
              'QR scan will start automatically.',
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
                  style: const TextStyle(color: Colors.redAccent, fontSize: 13),
                ),
                onPressed: (_canDeclareCurrentMissing && !_pendingServer)
                    ? _onDeclareCurrentMissing
                    : null,
              ),
            ),
          ],
        );
      case _Step.scanning:
        return _stepContent(
          icon: Icons.qr_code_scanner,
          color: Colors.blueAccent,
          title: 'Scanning for QR code…',
          subtitle: 'Hold the QR code under the camera and keep it still. '
              'Tracking will start as soon as it is found.',
          loading: !_noQrButtonVisible,
          error: _scanError,
          actions: [
            if (_noQrButtonVisible) ...[
              OutlinedButton.icon(
                icon: const Icon(Icons.hide_image_outlined, size: 18),
                label: const Text('No QR code on this object'),
                style: OutlinedButton.styleFrom(
                    minimumSize: const Size(double.infinity, 48),
                    side: const BorderSide(color: Colors.white30)),
                onPressed: _pendingServer ? null : _onNoQrCode,
              ),
              _changePhoneBtn(),
            ],
          ],
        );
      case _Step.trackingAdmin:
        return _stepContent(
          icon: Icons.my_location_outlined,
          color: Colors.greenAccent,
          title: 'Move phone to ${_slotLabel(_expectedLid)}',
          subtitle: 'Move it directly to the slot — or into a staging zone '
              'if the slot is occupied. The system detects both automatically.',
          error: _scanError,
          actions: [
            _TrackingQrBadge(qrVisible: _qrVisible),
          ],
        );
      case _Step.autoStaged:
        return _stepContent(
          icon: Icons.hourglass_top,
          color: Colors.amber,
          title: 'Phone staged — loading next step…',
          subtitle: 'The system detected the phone in the staging zone and is '
              'selecting the next phone to handle.',
          loading: true,
          actions: const [],
        );
      case _Step.unstageNext:
        final pid = _staged.isNotEmpty ? _staged.first : '?';
        final lid = _mismatchMap[pid] ?? 0;
        return _stepContent(
          icon: Icons.unarchive_outlined,
          color: Colors.tealAccent,
          title: 'Retrieve ${_displayPid(pid)} from staging',
          subtitle: 'Pick it up from the staging zone. '
              'Tracking to slot ${lid + 1} will start automatically.',
          error: _scanError,
          actions: [
            _primaryBtn("I've retrieved it from staging", Colors.teal, _onUnstageNext),
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
            onPressed: () => Navigator.of(context).pop(
                _sessionWasOpened ? false : null),
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
    final unresolved = (_summary?['unresolved']       as List?)?.length ?? 0;

    final icon  = _forceClosed
        ? Icons.cancel_outlined
        : Icons.check_circle_outline;
    final iconColor = _forceClosed ? Colors.orange : Colors.greenAccent;
    final title = _forceClosed
        ? 'Session ended early'
        : 'All done! Have a nice day \u{1F389}';

    return Column(mainAxisSize: MainAxisSize.min, children: [
      Icon(icon, color: iconColor, size: 48),
      const SizedBox(height: 14),
      Text(title,
          textAlign: TextAlign.center,
          style: const TextStyle(
              fontSize: 20, fontWeight: FontWeight.bold, color: Colors.white)),
      const SizedBox(height: 14),
      Wrap(
          spacing: 8, runSpacing: 6, alignment: WrapAlignment.center,
          children: [
            if (resolved   > 0) _summaryChip('$resolved resolved', Colors.green),
            if (missing    > 0) _summaryChip('$missing missing', Colors.red),
            if (depositing > 0) _summaryChip('$depositing to deposit', Colors.indigo),
            if (unresolved > 0) _summaryChip('$unresolved unresolved', Colors.orange),
          ]),
      if (_forceClosed) ...[
        const SizedBox(height: 12),
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
          decoration: BoxDecoration(
            color: Colors.orange.withOpacity(0.15),
            borderRadius: BorderRadius.circular(10),
            border: Border.all(color: Colors.orange.withOpacity(0.4)),
          ),
          child: const Row(children: [
            Icon(Icons.folder_open_outlined, color: Colors.orange, size: 18),
            SizedBox(width: 10),
            Expanded(
                child: Text(
                    'Evidence saved as NotFullyResolved. '
                    'Unresolved mismatches will re-alarm.',
                    style: TextStyle(color: Colors.orange, fontSize: 13))),
          ]),
        ),
      ] else if (_evidenceKept) ...[
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
      _primaryBtn('Close', _forceClosed ? Colors.deepOrangeAccent : Colors.green,
              () => Navigator.of(context).pop(_forceClosed ? false : true)),
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
            () => Navigator.of(context).pop(
                _sessionWasOpened ? false : null)),
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

  Widget _forceCloseButton() {
    // Safe close: blocked if phone in hand or phones staged.
    // Unsafe close (debug): always works, shown via long-press.
    return GestureDetector(
      onLongPress: () => _onForceClose(safe: false),
      child: TextButton.icon(
        icon: const Icon(Icons.close, size: 14, color: Colors.white38),
        label: const Text('End session',
            style: TextStyle(color: Colors.white38, fontSize: 12)),
        style: TextButton.styleFrom(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
          minimumSize: Size.zero,
          tapTargetSize: MaterialTapTargetSize.shrinkWrap,
        ),
        onPressed: _pendingServer ? null : () => _onForceClose(safe: true),
      ),
    );
  }

  Future<void> _onForceClose({required bool safe}) async {
    final hasPhoneInHand = _step == _Step.scanning || _step == _Step.trackingAdmin;
    final hasStaged      = _staged.isNotEmpty;
    final hasRemaining   = _remaining.isNotEmpty;

    String warningText;
    if (!safe) {
      warningText = 'DEBUG: Force-close regardless of state.\n'
          'All evidence will be saved as NotFullyResolved.';
    } else if (hasPhoneInHand) {
      // safe close is blocked server-side too, but show friendly message
      warningText = 'You have a phone in hand. Put it down first, '
          'then close the session.';
      if (mounted) {
        setState(() => _scanError = warningText);
      }
      return;
    } else if (hasStaged) {
      warningText = 'There are staged phones. Resolve them first, '
          'then close the session.';
      if (mounted) setState(() => _scanError = warningText);
      return;
    } else {
      warningText = hasRemaining
          ? 'End session now?\n\n'
            '${_remaining.length} mismatch(es) will remain unresolved.\n'
            'Evidence will be saved as NotFullyResolved.'
          : 'End session early? All resolved mismatches will be saved.';
    }

    final confirm = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        backgroundColor: const Color(0xFF1C1C1E),
        title: Text(
          safe ? 'End session' : 'Force close (debug)',
          style: const TextStyle(color: Colors.white),
        ),
        content: Text(warningText,
            style: const TextStyle(color: Colors.white70, fontSize: 13)),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Cancel',
                style: TextStyle(color: Colors.white54)),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(
              backgroundColor: safe ? Colors.deepOrangeAccent : Colors.red,
            ),
            onPressed: () => Navigator.pop(context, true),
            child: Text(safe ? 'End session' : 'Force close'),
          ),
        ],
      ),
    );

    if (confirm != true || !mounted) return;
    _waitForServer();
    _socket.adminForceClose(safe: safe);
  }

  String _nextLabel() =>
      (_remaining.isEmpty && _staged.isEmpty) ? 'Finish Session' : 'Next';
}

// ── QR status badge — only this widget rebuilds on tracking_update ────────────

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
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
      decoration: BoxDecoration(
        color: color.withOpacity(0.12),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: color.withOpacity(0.4)),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        Icon(icon, color: color, size: 20),
        const SizedBox(width: 8),
        Flexible(
          child: Text(label,
              style: TextStyle(
                  color: color, fontSize: 13, fontWeight: FontWeight.w500)),
        ),
      ]),
    );
  }
}

// ── Session progress bar overlay (Positioned, StatelessWidget) ────────────────

class _TopProgressBar extends StatelessWidget {
  final int total;
  final int done;
  final bool isRecording;
  const _TopProgressBar({
    required this.total,
    required this.done,
    required this.isRecording,
  });

  static Widget _badge(IconData icon, String label, Color bg) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
    decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(5)),
    child: Row(mainAxisSize: MainAxisSize.min, children: [
      Icon(icon, color: Colors.white, size: 11),
      const SizedBox(width: 4),
      Text(label,
          style: const TextStyle(
              color: Colors.white, fontSize: 10, fontWeight: FontWeight.w600)),
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
            begin: Alignment.topCenter,
            end: Alignment.bottomCenter,
            colors: [Colors.black87, Colors.transparent],
          ),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
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
                  value: pct,
                  minHeight: 3,
                  backgroundColor: Colors.white24,
                  valueColor: AlwaysStoppedAnimation(
                      done >= total ? Colors.green : Colors.deepOrangeAccent),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}