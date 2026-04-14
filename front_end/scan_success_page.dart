import 'package:flutter/material.dart';
import 'api_service.dart';
import 'socket_service.dart';

// ── Shared location label helper ─────────────────────────
/// "slot N (row R, col C)" — lid is 0-based; x = row, y = col.
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
  bool _loading = true;
  List<dynamic> _phones = [];

  @override
  void initState() {
    super.initState();
    _loadPhones();
  }

  @override
  void dispose() {
    _socketService.clearDvwCallbacks();
    super.dispose();
  }

  Future<void> _loadPhones() async {
    final data = await ApiService.getPhones(widget.sid);
    if (!mounted) return;
    setState(() {
      _phones  = data ?? [];
      _loading = false;
    });
  }

  void _startOperation(String pid, bool isDeposit) {
    if (isDeposit) {
      _socketService.deposit(pid);
    } else {
      _socketService.withdraw(pid);
    }
    showModalBottomSheet(
      context: context,
      isDismissible: false,
      enableDrag: false,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (_) => DVWBottomSheet(
        pid: pid,
        isDeposit: isDeposit,
        socketService: _socketService,
        onComplete: _loadPhones,
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
                    style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
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
              label: 'Take',
              color: Colors.green,
              enabled: isStored,
              onPressed: () => _startOperation(pid, false),
            ),
            const SizedBox(height: 8),
            _ActionButton(
              label: 'Put',
              color: Colors.blue,
              enabled: !isStored,
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
        title: Text(widget.studentName),
        leading: const BackButton(),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _phones.isEmpty
              ? const Center(child: Text('No phones found'))
              : ListView.builder(
                  itemCount: _phones.length,
                  itemBuilder: (_, i) =>
                      _buildPhoneCard(_phones[i] as Map<String, dynamic>),
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
            backgroundColor: color, fixedSize: const Size(80, 36)),
        child: Text(label),
      );
}

// ══════════════════════════════════════════════════════════
// DVW BOTTOM SHEET  (exported — used by admin_menu too)
// ══════════════════════════════════════════════════════════

enum DvwStep {
  waiting,
  autoScanning,
  tracking,
  success,
  error,
}

class DVWBottomSheet extends StatefulWidget {
  final String pid;
  final bool isDeposit;
  final SocketService socketService;
  final VoidCallback onComplete;

  const DVWBottomSheet({
    super.key,
    required this.pid,
    required this.isDeposit,
    required this.socketService,
    required this.onComplete,
  });

  @override
  State<DVWBottomSheet> createState() => _DVWBottomSheetState();
}

class _DVWBottomSheetState extends State<DVWBottomSheet> {
  DvwStep _step = DvwStep.waiting;
  int?    _slot;
  String? _errorText;
  bool    _qrVisible = true;

  @override
  void initState() {
    super.initState();
    _registerCallbacks();
  }

  @override
  void dispose() {
    widget.socketService.clearDvwCallbacks();
    super.dispose();
  }

  void _registerCallbacks() {
    widget.socketService.connect(
      onDepositWaiting: (data) {
        if (!mounted) return;
        setState(() {
          _slot = data['slot'] as int? ?? ((data['lid'] as int? ?? 0) + 1);
          _step = DvwStep.autoScanning;
        });
      },
      onWithdrawWaiting: (data) {
        if (!mounted) return;
        setState(() {
          _slot = data['slot'] as int? ?? ((data['lid'] as int? ?? 0) + 1);
          _step = DvwStep.autoScanning;
        });
      },
      onDepositResult:  (data) => _handleResult(data as Map),
      onWithdrawResult: (data) => _handleResult(data as Map),
      onOperationError: (data) {
        if (!mounted) return;
        setState(() {
          _step      = DvwStep.error;
          _errorText = (data as Map)['message'] as String? ?? 'Unknown error';
        });
      },
      onOperationCancelled: (_) {
        if (mounted) Navigator.of(context).pop();
      },
      onTrackingStarted: (_) {
        if (!mounted) return;
        setState(() {
          _step      = DvwStep.tracking;
          _qrVisible = true;
        });
      },
      onTrackingUpdate: (data) {
        if (!mounted) return;
        final visible = (data as Map)['qr_visible'] as bool? ?? true;
        if (visible != _qrVisible) setState(() => _qrVisible = visible);
      },
      onTrackingFailed: (data) {
        if (!mounted) return;
        setState(() {
          _step      = DvwStep.error;
          _errorText = _trackingMsg((data as Map)['reason'] as String? ?? '');
        });
      },
    );
  }

  void _handleResult(Map data) {
    if (!mounted) return;
    if (data['status'] == 'success') {
      setState(() => _step = DvwStep.success);
      widget.onComplete();
      Future.delayed(const Duration(seconds: 1), () {
        if (mounted) Navigator.of(context).pop();
      });
    } else {
      setState(() {
        _step      = DvwStep.error;
        _errorText = data['message'] as String? ?? 'Operation failed';
      });
    }
  }

  void _onCancel() {
    widget.socketService.cancelOperation();
    Navigator.of(context).pop();
  }

  static String _trackingMsg(String reason) {
    const map = {
      'qr_lost':       'QR code disappeared before reaching the slot. Keep it visible.',
      'out_of_frame':  'Phone left the camera view. Move directly toward the slot.',
      'timeout':       'Placement timed out. Please retry.',
      'detect_timeout':'Phone not detected. Make sure it enters the camera view.',
    };
    return map[reason] ?? 'Placement failed. Please retry.';
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(24, 20, 24, 32),
      child: Column(mainAxisSize: MainAxisSize.min, children: [
        Container(
          width: 40, height: 4,
          decoration: BoxDecoration(
              color: Colors.grey[300],
              borderRadius: BorderRadius.circular(2)),
        ),
        const SizedBox(height: 20),
        ..._buildContent(),
      ]),
    );
  }

  List<Widget> _buildContent() {
    switch (_step) {
      case DvwStep.waiting:
        return [
          const CircularProgressIndicator(),
          const SizedBox(height: 16),
          Text(
            widget.isDeposit ? 'Finding a free slot…' : 'Looking up your phone…',
            style: const TextStyle(fontSize: 16),
          ),
          const SizedBox(height: 24),
          _cancelBtn(),
        ];

      case DvwStep.autoScanning:
        final slotLabel = 'slot ${_slot ?? '?'}';
        final instruction = widget.isDeposit
            ? 'Hold the QR code under the top camera,\nthen carry the phone to $slotLabel.'
            : 'Remove your phone from $slotLabel,\nthen hold its QR code under the camera.';
        return [
          const SizedBox(
              width: 36, height: 36,
              child: CircularProgressIndicator(strokeWidth: 3)),
          const SizedBox(height: 16),
          Icon(
            widget.isDeposit ? Icons.login_outlined : Icons.logout_outlined,
            size: 40,
            color: widget.isDeposit ? Colors.blue : Colors.green,
          ),
          const SizedBox(height: 10),
          Text(instruction,
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
          const SizedBox(height: 8),
          Text('Scanning for QR code… (up to 15 s)',
              style: TextStyle(fontSize: 13, color: Colors.grey[500])),
          const SizedBox(height: 24),
          _cancelBtn(),
        ];

      case DvwStep.tracking:
        final qrColor = _qrVisible ? Colors.green : Colors.orange;
        final qrIcon  = _qrVisible ? Icons.qr_code_2 : Icons.qr_code_2_outlined;
        final qrLabel = _qrVisible
            ? 'QR code visible — keep it facing up'
            : 'QR code not detected — keep the QR visible!';
        return [
          const SizedBox(width: 36, height: 36,
              child: CircularProgressIndicator(strokeWidth: 3)),
          const SizedBox(height: 16),
          Text('Place phone in slot ${_slot ?? '?'}',
              style: const TextStyle(fontSize: 17, fontWeight: FontWeight.bold)),
          const SizedBox(height: 8),
          const Text(
            'Move the phone toward the slot.\nKeep the QR code visible until it lands.',
            textAlign: TextAlign.center,
            style: TextStyle(fontSize: 14, color: Colors.grey),
          ),
          const SizedBox(height: 16),
          _QrStatusBadge(color: qrColor, icon: qrIcon, label: qrLabel),
          const SizedBox(height: 24),
          _cancelBtn(),
        ];

      case DvwStep.success:
        final verb = widget.isDeposit ? 'stored' : 'retrieved';
        return [
          const Icon(Icons.check_circle_outline, size: 52, color: Colors.green),
          const SizedBox(height: 14),
          Text('Phone ${widget.pid} $verb successfully!',
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 17, fontWeight: FontWeight.bold)),
        ];

      case DvwStep.error:
        return [
          const Icon(Icons.error_outline, size: 48, color: Colors.red),
          const SizedBox(height: 14),
          Text(_errorText ?? 'Something went wrong.',
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 15)),
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

// ── QR status badge — extracted to prevent unnecessary decoration allocations ─

class _QrStatusBadge extends StatelessWidget {
  final Color color;
  final IconData icon;
  final String label;
  const _QrStatusBadge({
    required this.color,
    required this.icon,
    required this.label,
  });

  @override
  Widget build(BuildContext context) {
    return AnimatedContainer(
      duration: const Duration(milliseconds: 300),
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
      decoration: BoxDecoration(
        color: color.withOpacity(0.1),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: color.withOpacity(0.4)),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        Icon(icon, color: color, size: 20),
        const SizedBox(width: 8),
        Flexible(
          child: Text(
            label,
            style: TextStyle(
                color: color, fontSize: 13, fontWeight: FontWeight.w500),
          ),
        ),
      ]),
    );
  }
}