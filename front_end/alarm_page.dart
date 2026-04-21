import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'socket_service.dart';
import 'api_service.dart';
import 'admin_resolution_page.dart';

class AlarmPage extends StatefulWidget {
  final List<dynamic> initialMismatches;
  final RTCVideoRenderer mainRenderer;
  final bool Function() isMainConnected;

  const AlarmPage({
    super.key,
    required this.initialMismatches,
    required this.mainRenderer,
    required this.isMainConnected,
  });

  @override
  State<AlarmPage> createState() => _AlarmPageState();
}

class _AlarmPageState extends State<AlarmPage>
    with SingleTickerProviderStateMixin {
  final _socketService = SocketService();
  final _passwordController = TextEditingController();

  List<dynamic> _mismatches = [];
  bool _cleared = false;
  bool _loading = false;
  String? _errorMessage;
  bool _authenticated = false;

  final RTCVideoRenderer _adminRenderer = RTCVideoRenderer();
  RTCPeerConnection? _adminPc;
  bool _adminVideoConnected = false;

  bool _disposed = false;
  bool _resolutionInProgress = false;

  // Pending auto-pop timer. Kept as a field so it can be cancelled if the
  // alarm re-triggers before the delay expires (alarm fires per-slot so it can
  // briefly clear then re-fire within ~200 ms — without this the page would
  // dismiss itself before the admin has a chance to authenticate).
  Timer? _autoPop;

  // Pre-connect future — capped at 6 s via .timeout().
  // If pre-connect fails or times out, AdminResolutionPage starts its own
  // connection (inheritedPeerConnection = null, which it already handles).
  Future<void>? _preConnectFuture;

  late AnimationController _pulseCtrl;
  late Animation<double> _pulseAnim;

  // ── Display helpers ───────────────────────────────────

  static String _slotLabel(dynamic lid, {dynamic x, dynamic y}) {
    final slot = (lid as num).toInt() + 1;
    if (x != null && y != null) return 'slot $slot (row $x, col $y)';
    return 'slot $slot';
  }

  static String _pidLabel(dynamic pid) {
    final s = pid.toString();
    if (s.startsWith('unknown-')) return 'Unknown object';
    if (s.length > 20) return '…${s.substring(s.length - 12)}';
    return s;
  }

  // ── Lifecycle ─────────────────────────────────────────

  @override
  void initState() {
    super.initState();
    _mismatches = List.from(widget.initialMismatches);

    _pulseCtrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 900),
    )..repeat(reverse: true);
    _pulseAnim = Tween<double>(begin: 0.6, end: 1.0).animate(
      CurvedAnimation(parent: _pulseCtrl, curve: Curves.easeInOut),
    );

    // Start admin video pre-connect using only 1 attempt (not 5) so it
    // fails fast. The whole chain is also capped at 6 s so _onAuthSuccess
    // is never blocked indefinitely waiting for a slow/unavailable server.
    _preConnectFuture = _adminRenderer
        .initialize()
        .then((_) => _startAdminVideo())
        .timeout(const Duration(seconds: 6), onTimeout: () {});

    _connectSocket();
  }

  @override
  void dispose() {
    _disposed = true;
    _autoPop?.cancel();
    _pulseCtrl.dispose();
    _passwordController.dispose();
    _adminPc?.onTrack = null;
    _adminPc?.onConnectionState = null;
    _adminPc?.close();
    _adminRenderer.srcObject = null;
    _adminRenderer.dispose();
    ApiService.cancelAdmin();
    super.dispose();
  }

  // ── Admin camera ──────────────────────────────────────

  Future<void> _startAdminVideo() async {
    if (_disposed) return;
    try {
      await ApiService.cancelAdmin();
      _adminPc = await createPeerConnection({
        'iceServers': [
          {'urls': 'stun:stun.l.google.com:19302'}
        ],
      });
      _adminPc!.onTrack = (event) {
        if (_disposed || !mounted) return;
        if (event.streams.isNotEmpty) {
          setState(() {
            _adminRenderer.srcObject = event.streams[0];
            _adminVideoConnected = true;
          });
        }
      };
      _adminPc!.onConnectionState = (state) async {
        if (_disposed) return;
        if (state == RTCPeerConnectionState.RTCPeerConnectionStateDisconnected ||
            state == RTCPeerConnectionState.RTCPeerConnectionStateFailed) {
          if (mounted) setState(() => _adminVideoConnected = false);
          await _adminPc?.close();
          _adminPc = null;
          await Future.delayed(const Duration(seconds: 2));
          if (!_disposed) {
            _adminRenderer.srcObject = null;
            await _startAdminVideo();
          }
        }
      };
      final offer = await _adminPc!.createOffer(
          {'offerToReceiveVideo': true, 'offerToReceiveAudio': false});
      await _adminPc!.setLocalDescription(offer);
      // Single attempt only — no retries. If the server isn't ready the 6 s
      // timeout on _preConnectFuture will fire and we proceed without the PC.
      final sdp = await ApiService.sendOffer(offer.sdp!, mode: 'admin',
          maxRetries: 1);
      if (sdp != null && !_disposed) {
        await _adminPc!
            .setRemoteDescription(RTCSessionDescription(sdp, 'answer'));
      }
    } catch (_) {
      if (mounted) setState(() => _adminVideoConnected = false);
    }
  }

  RTCPeerConnection? _detachAdminPc() {
    final pc = _adminPc;
    _adminPc?.onTrack = null;
    _adminPc?.onConnectionState = null;
    _adminPc = null;
    _adminRenderer.srcObject = null;
    if (mounted) setState(() => _adminVideoConnected = false);
    return pc;
  }

  // ── Socket ────────────────────────────────────────────

  void _connectSocket() {
    _socketService.connect(
      onAlarmUpdated: (data) {
        if (!mounted) return;
        // The alarm re-fired (new or updated mismatch) — cancel any pending
        // auto-pop that was scheduled when the alarm briefly cleared.
        _autoPop?.cancel();
        _autoPop = null;
        setState(() {
          _cleared = false;
          _mismatches = data["mismatches"] ?? [];
        });
      },
      onAlarmCleared: (_) {
        if (!mounted) return;
        setState(() => _cleared = true);
        _maybeAutoPop();
      },
      // onAlarmStatus fires when we call requestAlarmStatus() below.
      // If alarm already cleared before this page opened, pop immediately.
      onAlarmStatus: (data) {
        if (!mounted) return;
        if (data["active"] != true) {
          setState(() => _cleared = true);
          _maybeAutoPop(delay: const Duration(milliseconds: 300));
        }
      },
      onAlarmAcknowledgeResult: (data) {
        if (!mounted) return;
        setState(() => _loading = false);
        if (data["status"] == "success") {
          _onAuthSuccess();
        } else {
          setState(() => _errorMessage = "Wrong password");
        }
      },
    );
    // Request current alarm status so we self-dismiss if the alarm was already
    // cleared before this page was pushed (common after a page refresh).
    _socketService.requestAlarmStatus();
  }

  // Auto-pop only when the admin hasn't started engaging with the auth flow.
  // Uses a stored Timer so it can be cancelled if the alarm re-fires before
  // the delay expires — without this, a brief alarm clear followed by a
  // re-trigger would dismiss the page while the admin is still authenticating.
  void _maybeAutoPop({Duration delay = const Duration(milliseconds: 800)}) {
    final adminEngaged = _resolutionInProgress ||
        _loading ||
        _authenticated ||
        _passwordController.text.trim().isNotEmpty;
    if (!adminEngaged) {
      _autoPop?.cancel();
      _autoPop = Timer(delay, () {
        if (mounted) Navigator.of(context).pop();
      });
    }
  }

  // ── Auth ──────────────────────────────────────────────

  void _submitPassword() {
    final pw = _passwordController.text.trim();
    if (pw.isEmpty) return;
    setState(() {
      _loading = true;
      _errorMessage = null;
    });
    _socketService.acknowledgeAlarm(pw);
  }

  void _onAuthSuccess() {
    setState(() => _authenticated = true);
    // _preConnectFuture is already capped at 6 s — this never blocks long.
    (_preConnectFuture ?? Future.value()).then((_) {
      if (mounted) _openResolution();
    });
  }

  Future<void> _openResolution() async {
    final existingPc = _detachAdminPc();
    _resolutionInProgress = true;

    final resolved = await Navigator.of(context).push<bool>(
      MaterialPageRoute(
        builder: (_) => AdminResolutionPage(
          password: _passwordController.text.trim(),
          mismatches: List.from(_mismatches),
          // May be null if pre-connect timed out — AdminResolutionPage
          // handles null via _startVideo() fallback in _initVideo().
          inheritedPeerConnection: existingPc,
        ),
      ),
    );

    _resolutionInProgress = false;
    if (!mounted) return;

    if (resolved == true) {
      // Session completed cleanly — dismiss the alarm page too.
      Navigator.of(context).pop();
      return;
    }

    if (resolved == false) {
      // Session was opened on the server but the admin abandoned it
      // (e.g. closed the page mid-resolution). Restart the alarm sound
      // so a subsequent admin knows resolution is still needed.
      _socketService.unsilenceAlarm();
    }
    // resolved == null means the session never opened (wrong password,
    // server error before open, session_already_active, etc.).
    // In that case we intentionally do NOT unsilence — the alarm is already
    // silenced and no resolution was attempted, so there is nothing to revert.

    setState(() => _authenticated = false);
    if (_cleared && mounted) Navigator.of(context).pop();
  }

  // ── Build ─────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: _cleared,
      child: Scaffold(
        backgroundColor: const Color(0xFF0D0D0F),
        body: SafeArea(
          child: Column(
            children: [
              Expanded(child: _buildCameraPanel()),
              _cleared ? _buildClearedView() : _buildAlarmContent(),
            ],
          ),
        ),
      ),
    );
  }

  // ── Camera panel ──────────────────────────────────────

  Widget _buildCameraPanel() {
    final bool showAdmin = _authenticated;
    final renderer  = showAdmin ? _adminRenderer : widget.mainRenderer;
    final connected = showAdmin ? _adminVideoConnected : widget.isMainConnected();
    final camLabel  = showAdmin ? 'TOP CAM' : 'FRONT CAM';
    final camIcon   = showAdmin ? Icons.videocam_outlined : Icons.face_outlined;

    return Stack(
      fit: StackFit.expand,
      children: [
        connected && renderer.srcObject != null
            ? RepaintBoundary(
                child: RTCVideoView(renderer,
                  objectFit: RTCVideoViewObjectFit.RTCVideoViewObjectFitContain))

            : Container(
                color: Colors.black,
                child: Center(
                  child: Column(mainAxisSize: MainAxisSize.min, children: [
                    const SizedBox(
                        width: 22, height: 22,
                        child: CircularProgressIndicator(
                            strokeWidth: 2, color: Colors.white24)),
                    const SizedBox(height: 8),
                    Text('Connecting $camLabel…',
                        style: const TextStyle(
                            color: Colors.white38, fontSize: 12)),
                  ]),
                ),
              ),
        // Static camera-type badge
        Positioned(
          bottom: 10, left: 12,
          child: _camBadge(camIcon, camLabel),
        ),
        // Animated REC badge — scoped so only this widget rebuilds on pulse
        Positioned(
          bottom: 10, right: 12,
          child: AnimatedBuilder(
            animation: _pulseAnim,
            builder: (_, __) => Opacity(
              opacity: _pulseAnim.value,
              child: _camBadge(Icons.fiber_manual_record, 'REC',
                  dotColor: const Color(0xFFE5484D)),
            ),
          ),
        ),
      ],
    );
  }

  Widget _camBadge(IconData icon, String label, {Color? dotColor}) =>
      Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(
          color: Colors.black.withOpacity(0.55),
          borderRadius: BorderRadius.circular(6),
          border: Border.all(color: Colors.white.withOpacity(0.08)),
        ),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          Icon(icon, color: dotColor ?? Colors.white38, size: 12),
          const SizedBox(width: 5),
          Text(label,
              style: const TextStyle(
                  color: Colors.white54, fontSize: 11,
                  fontWeight: FontWeight.w500, letterSpacing: 0.3)),
        ]),
      );

  // ── Cleared ───────────────────────────────────────────

  Widget _buildClearedView() => Container(
    padding: const EdgeInsets.symmetric(vertical: 32),
    child: Center(
      child: Column(mainAxisSize: MainAxisSize.min, children: [
        Container(
          width: 72, height: 72,
          decoration: BoxDecoration(
            color: const Color(0xFF0F2318),
            shape: BoxShape.circle,
            border: Border.all(
                color: const Color(0xFF30A46C).withOpacity(0.4), width: 1.5),
          ),
          child: const Icon(Icons.check_rounded,
              color: Color(0xFF30A46C), size: 36),
        ),
        const SizedBox(height: 20),
        const Text('Alarm cleared',
            style: TextStyle(fontSize: 22, fontWeight: FontWeight.w600,
                color: Colors.white)),
        const SizedBox(height: 6),
        const Text('All slots are back to their expected state.',
            style: TextStyle(color: Colors.white38, fontSize: 13)),
      ]),
    ),
  );

  // ── Alarm content ─────────────────────────────────────

  Widget _buildAlarmContent() => SingleChildScrollView(
    padding: const EdgeInsets.fromLTRB(16, 16, 16, 24),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _buildAlarmHeader(),
        if (_mismatches.isNotEmpty) ...[
          const SizedBox(height: 12),
          _buildMismatchList(),
        ],
        const SizedBox(height: 12),
        _buildAuthCard(),
      ],
    ),
  );

  Widget _buildAlarmHeader() => Container(
    padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
    decoration: BoxDecoration(
      color: const Color(0xFF1F1315),
      borderRadius: BorderRadius.circular(14),
      border: Border.all(
          color: const Color(0xFFE5484D).withOpacity(0.2), width: 1),
    ),
    child: Row(children: [
      // Only this icon pulses — scope AnimatedBuilder so the text/count
      // widgets are not rebuilt on every animation tick.
      AnimatedBuilder(
        animation: _pulseAnim,
        builder: (_, __) => Opacity(
          opacity: _pulseAnim.value,
          child: Container(
            width: 40, height: 40,
            decoration: BoxDecoration(
              color: const Color(0xFFE5484D).withOpacity(0.12),
              shape: BoxShape.circle,
            ),
            child: const Icon(Icons.warning_rounded,
                color: Color(0xFFE5484D), size: 22),
          ),
        ),
      ),
      const SizedBox(width: 14),
      Expanded(
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Text('Alarm active',
              style: TextStyle(fontSize: 16, fontWeight: FontWeight.w600,
                  color: Color(0xFFFF6B6B))),
          const SizedBox(height: 2),
          Text(
            '${_mismatches.length} slot '
                'mismatch${_mismatches.length == 1 ? '' : 'es'} detected',
            style: const TextStyle(color: Colors.white38, fontSize: 13),
          ),
        ]),
      ),
    ]),
  );

  Widget _buildMismatchList() => Container(
    decoration: BoxDecoration(
      color: const Color(0xFF1C1C1E),
      borderRadius: BorderRadius.circular(14),
      border: Border.all(color: Colors.white.withOpacity(0.07), width: 1),
    ),
    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Padding(
        padding: const EdgeInsets.fromLTRB(16, 14, 16, 8),
        child: Text('MISMATCHED SLOTS',
            style: TextStyle(fontSize: 11, fontWeight: FontWeight.w600,
                color: Colors.white.withOpacity(0.3), letterSpacing: 0.9)),
      ),
      const Divider(height: 1, color: Colors.white10),
      ConstrainedBox(
        constraints: const BoxConstraints(maxHeight: 160),
        child: ListView.separated(
          shrinkWrap: true,
          padding: EdgeInsets.zero,
          itemCount: _mismatches.length,
          separatorBuilder: (_, __) =>
          const Divider(height: 1, color: Colors.white10),
          itemBuilder: (_, i) {
            final m   = _mismatches[i];
            final pid = m[0];
            final lid = m[1];
            final x   = m.length > 2 ? m[2] : null;
            final y   = m.length > 3 ? m[3] : null;
            final isUnknown = pid.toString().startsWith('unknown-');
            return Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 11),
              child: Row(children: [
                Container(
                  width: 34, height: 34,
                  decoration: BoxDecoration(
                    color: const Color(0xFFE5484D).withOpacity(0.1),
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Icon(
                      isUnknown
                          ? Icons.help_outline_rounded
                          : Icons.smartphone,
                      color: const Color(0xFFFF6B6B), size: 18),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(_pidLabel(pid),
                          style: const TextStyle(color: Colors.white,
                              fontSize: 13, fontWeight: FontWeight.w500),
                          overflow: TextOverflow.ellipsis),
                      const SizedBox(height: 2),
                      Text('Expected in ${_slotLabel(lid, x: x, y: y)}',
                          style: const TextStyle(
                              color: Colors.white38, fontSize: 12)),
                    ],
                  ),
                ),
              ]),
            );
          },
        ),
      ),
    ]),
  );

  Widget _buildAuthCard() => Container(
    padding: const EdgeInsets.all(20),
    decoration: BoxDecoration(
      color: const Color(0xFF1C1C1E),
      borderRadius: BorderRadius.circular(16),
      border: Border.all(color: Colors.white.withOpacity(0.07), width: 1),
    ),
    child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      const Text('Admin authentication required',
          style: TextStyle(fontSize: 14, fontWeight: FontWeight.w600,
              color: Colors.white)),
      const SizedBox(height: 4),
      const Text(
        'Enter your password to silence the alarm and begin guided resolution.',
        style: TextStyle(color: Colors.white38, fontSize: 12, height: 1.5),
      ),
      const SizedBox(height: 16),
      TextField(
        controller: _passwordController,
        obscureText: true,
        style: const TextStyle(color: Colors.white),
        decoration: InputDecoration(
          labelText: 'Admin password',
          labelStyle: const TextStyle(color: Colors.white38),
          errorText: _errorMessage,
          errorStyle: const TextStyle(color: Color(0xFFFF6B6B)),
          prefixIcon: const Icon(Icons.lock_outline,
              color: Colors.white38, size: 20),
          filled: true,
          fillColor: Colors.white.withOpacity(0.04),
          border: OutlineInputBorder(
              borderRadius: BorderRadius.circular(12),
              borderSide: BorderSide(color: Colors.white.withOpacity(0.12))),
          enabledBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(12),
              borderSide: BorderSide(color: Colors.white.withOpacity(0.12))),
          focusedBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(12),
              borderSide: const BorderSide(
                  color: Color(0xFFE5484D), width: 1.5)),
          errorBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(12),
              borderSide: const BorderSide(color: Color(0xFFE5484D))),
          focusedErrorBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(12),
              borderSide: const BorderSide(
                  color: Color(0xFFE5484D), width: 1.5)),
        ),
        onSubmitted: (_) => _submitPassword(),
      ),
      const SizedBox(height: 14),
      ElevatedButton.icon(
        icon: _loading
            ? const SizedBox(
            width: 18, height: 18,
            child: CircularProgressIndicator(
                strokeWidth: 2, color: Colors.white))
            : const Icon(Icons.shield_outlined, size: 18),
        label: const Text('Silence & begin resolution',
            style: TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
        style: ElevatedButton.styleFrom(
          backgroundColor: const Color(0xFFE5484D),
          foregroundColor: Colors.white,
          padding: const EdgeInsets.symmetric(vertical: 15),
          shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(12)),
          elevation: 0,
        ),
        onPressed: _loading ? null : _submitPassword,
      ),
    ]),
  );
}