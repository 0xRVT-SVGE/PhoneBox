import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'socket_service.dart';
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
  final _socketService      = SocketService();
  final _passwordController = TextEditingController();

  List<dynamic> _mismatches = [];
  bool    _cleared              = false;
  bool    _loading              = false;
  String? _errorMessage;
  bool    _resolutionInProgress = false;

  Timer? _autoPop;

  late AnimationController _pulseCtrl;
  late Animation<double>   _pulseAnim;

  final List<StreamSubscription> _subs = [];

  // ── Helpers ───────────────────────────────────────────────────────────────

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

  // ── Lifecycle ─────────────────────────────────────────────────────────────

  @override
  void initState() {
    super.initState();
    _mismatches = List.from(widget.initialMismatches);

    _pulseCtrl = AnimationController(
      vsync:    this,
      duration: const Duration(milliseconds: 900),
    )..repeat(reverse: true);
    _pulseAnim = Tween<double>(begin: 0.6, end: 1.0).animate(
      CurvedAnimation(parent: _pulseCtrl, curve: Curves.easeInOut),
    );

    _connectSocket();
  }

  @override
  void dispose() {
    _autoPop?.cancel();
    _pulseCtrl.dispose();
    _passwordController.dispose();
    for (final s in _subs) s.cancel();
    super.dispose();
  }

  // ── Socket ────────────────────────────────────────────────────────────────

  void _connectSocket() {
    _socketService.connect();

    _subs.add(_socketService.onAlarmUpdated.listen((data) {
      if (!mounted) return;
      _autoPop?.cancel();
      _autoPop = null;
      setState(() {
        _cleared    = false;
        _mismatches = data['mismatches'] ?? [];
      });
    }));

    _subs.add(_socketService.onAlarmCleared.listen((_) {
      if (!mounted) return;
      setState(() => _cleared = true);
      _maybeAutoPop();
    }));

    _subs.add(_socketService.onAlarmStatus.listen((data) {
      if (!mounted) return;
      if (data['active'] != true) {
        setState(() => _cleared = true);
        _maybeAutoPop(delay: const Duration(milliseconds: 300));
      }
    }));

    _subs.add(_socketService.onAlarmAcknowledgeResult.listen((data) {
      if (!mounted) return;
      setState(() => _loading = false);
      if (data['status'] == 'success') {
        _onAuthSuccess();
      } else {
        setState(() => _errorMessage = 'Wrong password');
      }
    }));

    _socketService.requestAlarmStatus();
  }

  void _maybeAutoPop({Duration delay = const Duration(milliseconds: 800)}) {
    _autoPop?.cancel();
    _autoPop = Timer(delay, () {
      final adminEngaged = _resolutionInProgress ||
          _loading ||
          _passwordController.text.trim().isNotEmpty;
      if (!adminEngaged && mounted) Navigator.of(context).pop();
    });
  }

  // ── Auth ──────────────────────────────────────────────────────────────────

  void _submitPassword() {
    final pw = _passwordController.text.trim();
    if (pw.isEmpty) return;
    setState(() {
      _loading      = true;
      _errorMessage = null;
    });
    _socketService.acknowledgeAlarm(pw);
  }

  void _onAuthSuccess() => _openResolution();

  Future<void> _openResolution() async {
    _resolutionInProgress = true;

    final resolved = await Navigator.of(context).push<bool>(
      MaterialPageRoute(
        builder: (_) => AdminResolutionPage(
          password:                _passwordController.text.trim(),
          mismatches:              List.from(_mismatches),
          inheritedPeerConnection: null,
        ),
      ),
    );

    _resolutionInProgress = false;
    if (!mounted) return;

    if (resolved == true) {
      Navigator.of(context).pop();
      return;
    }
    if (resolved == false) _socketService.unsilenceAlarm();
    if (_cleared && mounted) Navigator.of(context).pop();
  }

  // ── Build ─────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: _cleared,
      child: Scaffold(
        backgroundColor: const Color(0xFF0D0D0F),
        body: SafeArea(
          child: _cleared
              ? _buildClearedView()
              : Column(
                  children: [
                    Expanded(child: _buildCameraPanel()),
                    _buildScrollContent(),
                  ],
                ),
        ),
      ),
    );
  }

  // ── Front camera panel ───────────────────────────────────────────────────

  Widget _buildCameraPanel() {
    final connected = widget.isMainConnected() &&
        widget.mainRenderer.srcObject != null;
    return Stack(
      fit: StackFit.expand,
      children: [
        connected
            ? RepaintBoundary(
                child: RTCVideoView(
                  widget.mainRenderer,
                  objectFit:
                      RTCVideoViewObjectFit.RTCVideoViewObjectFitContain,
                ),
              )
            : Container(
                color: Colors.black,
                child: Center(
                  child: Column(mainAxisSize: MainAxisSize.min, children: [
                    Icon(Icons.videocam_off_outlined,
                        color: Colors.white.withOpacity(0.18), size: 36),
                    const SizedBox(height: 10),
                    Text('Front camera',
                        style: TextStyle(
                            color: Colors.white.withOpacity(0.25),
                            fontSize: 13)),
                  ]),
                ),
              ),
        // Thin red tint at top to connect visually with alarm theme
        Positioned(
          top: 0, left: 0, right: 0,
          child: Container(
            height: 40,
            decoration: BoxDecoration(
              gradient: LinearGradient(
                begin: Alignment.topCenter,
                end:   Alignment.bottomCenter,
                colors: [
                  const Color(0xFFE5484D).withOpacity(0.18),
                  Colors.transparent,
                ],
              ),
            ),
          ),
        ),
        // REC badge
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
        // FRONT CAM badge
        Positioned(
          bottom: 10, left: 12,
          child: _camBadge(Icons.face_outlined, 'FRONT CAM'),
        ),
      ],
    );
  }

  Widget _camBadge(IconData icon, String label, {Color? dotColor}) =>
      Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(
          color:        Colors.black.withOpacity(0.55),
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

  // ── Cleared view ──────────────────────────────────────────────────────────

  Widget _buildClearedView() => Center(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Container(
            width: 72, height: 72,
            decoration: BoxDecoration(
              color:  const Color(0xFF0F2318),
              shape:  BoxShape.circle,
              border: Border.all(
                  color: const Color(0xFF30A46C).withOpacity(0.4),
                  width: 1.5),
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
      );

  // ── Scrollable auth content ───────────────────────────────────────────────

  Widget _buildScrollContent() => SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            _buildAlarmHeader(),
            if (_mismatches.isNotEmpty) ...[
              const SizedBox(height: 10),
              _buildMismatchList(),
            ],
            const SizedBox(height: 10),
            _buildAuthCard(),
          ],
        ),
      );

  Widget _buildAlarmHeader() => Container(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        decoration: BoxDecoration(
          color:        const Color(0xFF1F1315),
          borderRadius: BorderRadius.circular(14),
          border:       Border.all(
              color: const Color(0xFFE5484D).withOpacity(0.2), width: 1),
        ),
        child: Row(children: [
          AnimatedBuilder(
            animation: _pulseAnim,
            builder: (_, __) => Opacity(
              opacity: _pulseAnim.value,
              child: Container(
                width: 36, height: 36,
                decoration: BoxDecoration(
                  color:  const Color(0xFFE5484D).withOpacity(0.12),
                  shape:  BoxShape.circle,
                ),
                child: const Icon(Icons.warning_rounded,
                    color: Color(0xFFE5484D), size: 20),
              ),
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
                crossAxisAlignment: CrossAxisAlignment.start, children: [
              const Text('Alarm active',
                  style: TextStyle(fontSize: 15, fontWeight: FontWeight.w600,
                      color: Color(0xFFFF6B6B))),
              const SizedBox(height: 2),
              Text(
                '${_mismatches.length} slot '
                'mismatch${_mismatches.length == 1 ? '' : 'es'} detected',
                style: const TextStyle(color: Colors.white38, fontSize: 12),
              ),
            ]),
          ),
        ]),
      );

  Widget _buildMismatchList() => Container(
        decoration: BoxDecoration(
          color:        const Color(0xFF1C1C1E),
          borderRadius: BorderRadius.circular(12),
          border:       Border.all(
              color: Colors.white.withOpacity(0.07), width: 1),
        ),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 12, 14, 6),
            child: Text('MISMATCHED SLOTS',
                style: TextStyle(
                    fontSize: 10, fontWeight: FontWeight.w600,
                    color: Colors.white.withOpacity(0.3),
                    letterSpacing: 0.9)),
          ),
          const Divider(height: 1, color: Colors.white10),
          ConstrainedBox(
            constraints: const BoxConstraints(maxHeight: 150),
            child: ListView.separated(
              shrinkWrap:       true,
              padding:          EdgeInsets.zero,
              physics:          const ClampingScrollPhysics(),
              itemCount:        _mismatches.length,
              separatorBuilder: (_, __) =>
                  const Divider(height: 1, color: Colors.white10),
              itemBuilder: (_, i) {
                final m         = _mismatches[i];
                final pid       = m[0];
                final lid       = m[1];
                final x         = m.length > 2 ? m[2] : null;
                final y         = m.length > 3 ? m[3] : null;
                final isUnknown = pid.toString().startsWith('unknown-');
                return Padding(
                  padding: const EdgeInsets.symmetric(
                      horizontal: 14, vertical: 10),
                  child: Row(children: [
                    Container(
                      width: 30, height: 30,
                      decoration: BoxDecoration(
                        color: const Color(0xFFE5484D).withOpacity(0.1),
                        borderRadius: BorderRadius.circular(7),
                      ),
                      child: Icon(
                          isUnknown
                              ? Icons.help_outline_rounded
                              : Icons.smartphone,
                          color: const Color(0xFFFF6B6B), size: 16),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                        Text(_pidLabel(pid),
                            style: const TextStyle(color: Colors.white,
                                fontSize: 12,
                                fontWeight: FontWeight.w500),
                            overflow: TextOverflow.ellipsis),
                        const SizedBox(height: 1),
                        Text('Expected in ${_slotLabel(lid, x: x, y: y)}',
                            style: const TextStyle(
                                color: Colors.white38, fontSize: 11)),
                      ]),
                    ),
                  ]),
                );
              },
            ),
          ),
        ]),
      );

  Widget _buildAuthCard() => Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color:        const Color(0xFF1C1C1E),
          borderRadius: BorderRadius.circular(16),
          border:       Border.all(
              color: Colors.white.withOpacity(0.07), width: 1),
        ),
        child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          const Text('Admin authentication required',
              style: TextStyle(fontSize: 14, fontWeight: FontWeight.w600,
                  color: Colors.white)),
          const SizedBox(height: 3),
          const Text(
            'Enter your password to silence the alarm and begin resolution.',
            style: TextStyle(color: Colors.white38, fontSize: 12, height: 1.5),
          ),
          const SizedBox(height: 14),
          TextField(
            controller:  _passwordController,
            obscureText: true,
            autofocus:   true,
            style:       const TextStyle(color: Colors.white),
            decoration:  InputDecoration(
              labelText:  'Admin password',
              labelStyle: const TextStyle(color: Colors.white38),
              errorText:  _errorMessage,
              errorStyle: const TextStyle(color: Color(0xFFFF6B6B)),
              prefixIcon: const Icon(Icons.lock_outline,
                  color: Colors.white38, size: 20),
              filled:    true,
              fillColor: Colors.white.withOpacity(0.04),
              border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                  borderSide:
                      BorderSide(color: Colors.white.withOpacity(0.12))),
              enabledBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                  borderSide:
                      BorderSide(color: Colors.white.withOpacity(0.12))),
              focusedBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                  borderSide: const BorderSide(
                      color: Color(0xFFE5484D), width: 1.5)),
              errorBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                  borderSide:
                      const BorderSide(color: Color(0xFFE5484D))),
              focusedErrorBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                  borderSide: const BorderSide(
                      color: Color(0xFFE5484D), width: 1.5)),
            ),
            onSubmitted: (_) => _submitPassword(),
          ),
          const SizedBox(height: 12),
          ElevatedButton.icon(
            icon: _loading
                ? const SizedBox(
                    width: 18, height: 18,
                    child: CircularProgressIndicator(
                        strokeWidth: 2, color: Colors.white))
                : const Icon(Icons.shield_outlined, size: 18),
            label: const Text('Silence & begin resolution',
                style: TextStyle(
                    fontSize: 14, fontWeight: FontWeight.w600)),
            style: ElevatedButton.styleFrom(
              backgroundColor: const Color(0xFFE5484D),
              foregroundColor: Colors.white,
              padding:         const EdgeInsets.symmetric(vertical: 14),
              shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12)),
              elevation: 0,
            ),
            onPressed: _loading ? null : _submitPassword,
          ),
        ]),
      );
}