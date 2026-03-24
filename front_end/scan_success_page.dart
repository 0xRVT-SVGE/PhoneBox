import 'package:flutter/material.dart';
import 'api_service.dart';
import 'socket_service.dart';

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

  Future<void> _loadPhones() async {
    final data = await ApiService.getPhones(widget.sid);
    if (!mounted) return;
    setState(() {
      _phones = data ?? [];
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
        onComplete: () {
          if (mounted) _loadPhones();
        },
      ),
    );
  }

  Widget _buildPhoneCard(Map<String, dynamic> p) {
    final pid      = p["pid"].toString();
    final model    = p["model"] ?? "Unknown Model";
    final isStored = p["is_stored"] == true;
    final location = (p["x"] != null && p["y"] != null)
        ? "Row ${p['x']}, Col ${p['y']}"
        : "N/A";

    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 10, horizontal: 14),
        child: Row(
          children: [
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(model, style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
                const SizedBox(height: 4),
                Text("PID: $pid", style: const TextStyle(fontSize: 12, color: Colors.grey)),
                Text("Location: $location"),
                Text(
                  isStored ? "📦 Stored" : "🎒 With you",
                  style: TextStyle(
                      color: isStored ? Colors.green[700] : Colors.blue[700],
                      fontWeight: FontWeight.w600),
                ),
              ]),
            ),
            Column(children: [
              ElevatedButton(
                onPressed: isStored ? () => _startOperation(pid, false) : null,
                style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.green,
                    fixedSize: const Size(80, 36)),
                child: const Text("Take"),
              ),
              const SizedBox(height: 8),
              ElevatedButton(
                onPressed: !isStored ? () => _startOperation(pid, true) : null,
                style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.blue,
                    fixedSize: const Size(80, 36)),
                child: const Text("Put"),
              ),
            ]),
          ],
        ),
      ),
    );
  }

  @override
  void dispose() {
    _socketService.clearDvwCallbacks();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(widget.studentName),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          onPressed: () => Navigator.pop(context),
        ),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _phones.isEmpty
          ? const Center(child: Text("No phones found"))
          : ListView.builder(
        itemCount: _phones.length,
        itemBuilder: (_, i) => _buildPhoneCard(_phones[i]),
      ),
    );
  }
}


// ──────────────────────────────────────────────────────────
// DVW BOTTOM SHEET
// ──────────────────────────────────────────────────────────

enum _DvwStep { waiting, readyToScan, scanning, success, error }

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
  _DvwStep _step = _DvwStep.waiting;
  int?     _lid;
  String?  _errorText;

  @override
  void initState() {
    super.initState();
    _registerCallbacks();
  }

  void _registerCallbacks() {
    widget.socketService.connect(
      onDepositWaiting: (data) {
        if (!mounted) return;
        setState(() {
          _lid  = data['lid'] as int?;
          _step = _DvwStep.readyToScan;
        });
      },
      onWithdrawWaiting: (data) {
        if (!mounted) return;
        setState(() {
          _lid  = data['lid'] as int?;
          _step = _DvwStep.readyToScan;
        });
      },
      onDepositResult: (data) => _handleResult(data),
      onWithdrawResult: (data) => _handleResult(data),
      onOperationError: (data) {
        if (!mounted) return;
        setState(() {
          _step = _DvwStep.error;
          _errorText = data['message'] ?? 'Unknown error';
        });
      },
    );
  }

  void _handleResult(Map data) {
    if (!mounted) return;
    if (data['status'] == 'success') {
      setState(() => _step = _DvwStep.success);
      widget.onComplete();
      Future.delayed(const Duration(seconds: 1), () {
        if (mounted) Navigator.of(context).pop();
      });
    } else {
      setState(() {
        _step = _DvwStep.error;
        _errorText = data['message'] ?? 'Operation failed';
      });
    }
  }

  void _onScanQr() {
    widget.socketService.qrScanned();
    setState(() => _step = _DvwStep.scanning);
  }

  @override
  void dispose() {
    widget.socketService.clearDvwCallbacks();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(24, 20, 24, 32),
      child: Column(mainAxisSize: MainAxisSize.min, children: [
        // Drag handle
        Container(width: 40, height: 4,
            decoration: BoxDecoration(
                color: Colors.grey[300],
                borderRadius: BorderRadius.circular(2))),
        const SizedBox(height: 20),
        ..._buildContent(),
      ]),
    );
  }

  List<Widget> _buildContent() {
    switch (_step) {
      case _DvwStep.waiting:
        return [
          const SizedBox(width: 36, height: 36,
              child: CircularProgressIndicator(strokeWidth: 3)),
          const SizedBox(height: 16),
          Text(
            widget.isDeposit ? "Finding a free slot..." : "Looking up your phone...",
            style: const TextStyle(fontSize: 16),
          ),
        ];

      case _DvwStep.readyToScan:
        final action = widget.isDeposit
            ? "Please hold your QR code under the top camera and tap Scan."
            : "Remove the phone from slot ${_lid ?? '?'}.";
        final scanInstruction = widget.isDeposit
            ? "Then place the phone in slot ${_lid ?? '?'}."
            : "Then hold its QR code under the top camera and tap Scan.";
        return [
          Icon(
            widget.isDeposit ? Icons.login_outlined : Icons.logout_outlined,
            size: 44,
            color: widget.isDeposit ? Colors.blue : Colors.green,
          ),
          const SizedBox(height: 14),
          Text(action,
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 17, fontWeight: FontWeight.bold)),
          const SizedBox(height: 8),
          Text(scanInstruction,
              textAlign: TextAlign.center,
              style: TextStyle(fontSize: 14, color: Colors.grey[600])),
          const SizedBox(height: 24),
          SizedBox(
            width: double.infinity,
            child: ElevatedButton.icon(
              icon: const Icon(Icons.qr_code_scanner),
              label: const Text("Scan QR Code", style: TextStyle(fontSize: 16)),
              style: ElevatedButton.styleFrom(
                  padding: const EdgeInsets.symmetric(vertical: 14),
                  backgroundColor:
                  widget.isDeposit ? Colors.blue : Colors.green),
              onPressed: _onScanQr,
            ),
          ),
          const SizedBox(height: 10),
          TextButton(
            onPressed: () => Navigator.of(context).pop(),
            child: const Text("Cancel"),
          ),
        ];

      case _DvwStep.scanning:
        return [
          const SizedBox(width: 36, height: 36,
              child: CircularProgressIndicator(strokeWidth: 3)),
          const SizedBox(height: 16),
          const Text("Scanning QR code...",
              style: TextStyle(fontSize: 16)),
          const SizedBox(height: 6),
          Text("Keep it still — up to 15 seconds",
              style: TextStyle(fontSize: 13, color: Colors.grey[500])),
        ];

      case _DvwStep.success:
        final verb = widget.isDeposit ? "stored" : "retrieved";
        return [
          const Icon(Icons.check_circle_outline, size: 52, color: Colors.green),
          const SizedBox(height: 14),
          Text("Phone ${widget.pid} $verb successfully!",
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 17, fontWeight: FontWeight.bold)),
        ];

      case _DvwStep.error:
        return [
          const Icon(Icons.error_outline, size: 48, color: Colors.red),
          const SizedBox(height: 14),
          Text(_errorText ?? "Something went wrong.",
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 16)),
          const SizedBox(height: 20),
          TextButton(
            onPressed: () => Navigator.of(context).pop(),
            child: const Text("Close"),
          ),
        ];
    }
  }
}