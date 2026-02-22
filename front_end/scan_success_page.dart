import 'package:flutter/material.dart';
import 'api_service.dart';
import 'socket_service.dart'; // ADD THIS IMPORT

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
  bool _loading = true;
  List<dynamic> _phones = [];
  String _depositStatus = "Not started";
  String? _testPid;

  final _socketService = SocketService(); // This should work now with import

  @override
  void initState() {
    super.initState();
    _loadPhones();
    _setupSocketListeners();
  }

  void _setupSocketListeners() {
    _socketService.connect(
      onDepositWaiting: (data) {
        print("📥 Flutter received: deposit_waiting_for_qr");
        print("Data: $data");
        if (!mounted) return;
        setState(() {
          _depositStatus = "Waiting for QR scan\nLID: ${data['lid']}";
        });
      },
      onDepositResult: (data) {
        print("📥 Flutter received: deposit_result");
        print("Data: $data");
        if (!mounted) return;
        setState(() {
          if (data['status'] == 'success') {
            _depositStatus = "✅ Success!\nLID: ${data['lid']}";
          } else {
            _depositStatus = "❌ Failed: ${data['message']}";
          }
        });
      },
      onOperationError: (data) {
        print("❌ Flutter received: operation_error");
        print("Data: $data");
        if (!mounted) return;
        setState(() {
          _depositStatus = "❌ Error: ${data['message']}";
        });
      },
    );
  }

  Future<void> _loadPhones() async {
    final data = await ApiService.getPhones(widget.sid);
    if (!mounted) return;

    setState(() {
      _phones = data ?? [];
      _loading = false;
      // Set first phone as test PID if available
      if (_phones.isNotEmpty) {
        _testPid = _phones[0]['pid'];
      }
    });
  }

  // TEST METHOD - Trigger deposit via WebSocket
  void _testDepositWebSocket() {
    if (_testPid == null) {
      setState(() => _depositStatus = "No phones available to test");
      return;
    }

    print("🧪 TEST: Starting deposit test for PID: $_testPid");
    setState(() => _depositStatus = "Testing deposit...");
    _socketService.deposit(_testPid!);
  }

  // TEST METHOD - Simulate QR scan
  void _testQRScan() {
    print("🧪 TEST: Simulating QR scan");
    _socketService.qrScanned();
  }

  Future<void> _takePhone(String pid) async {
    // Use WebSocket instead
    print("📤 Withdrawing phone: $pid");
    _socketService.withdraw(pid);
  }

  Future<void> _putPhone(String pid) async {
    // Use WebSocket instead
    print("📥 Depositing phone: $pid");
    _socketService.deposit(pid);
  }

  Widget _buildPhoneCard(Map<String, dynamic> p) {
    final pid = p["pid"];
    final model = p["model"] ?? "Unknown Model";
    final isStored = p["is_stored"] == true ? "Stored" : "With Student";
    final location = (p["x"] != null && p["y"] != null)
        ? "line= ${p['x']}, column= ${p['y']}"
        : "N/A";

    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 12),
        child: Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(model, style: const TextStyle(fontSize: 17, fontWeight: FontWeight.bold)),
                  const SizedBox(height: 4),
                  Text("PID: $pid", style: const TextStyle(fontSize: 12, color: Colors.grey)),
                  Text("Location: $location"),
                  Text("Status: $isStored"),
                ],
              ),
            ),
            Column(
              children: [
                ElevatedButton(
                  onPressed: isStored == "Stored" ? () => _takePhone(pid) : null,
                  style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.green,
                    minimumSize: const Size(70, 38),
                  ),
                  child: const Text("Take"),
                ),
                const SizedBox(height: 8),
                ElevatedButton(
                  onPressed: isStored == "With Student" ? () => _putPhone(pid) : null,
                  style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.blue,
                    minimumSize: const Size(70, 38),
                  ),
                  child: const Text("Put"),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text("Phones of ${widget.studentName}"),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          onPressed: () => Navigator.pop(context),
        ),
      ),
      body: Column(
        children: [
          // TEST SECTION - Remove this after testing
          Container(
            color: Colors.yellow[100],
            padding: const EdgeInsets.all(12),
            child: Column(
              children: [
                const Text(
                  "🧪 WebSocket Test Section",
                  style: TextStyle(fontWeight: FontWeight.bold, fontSize: 16),
                ),
                const SizedBox(height: 8),
                Text(
                  _depositStatus,
                  textAlign: TextAlign.center,
                  style: const TextStyle(fontSize: 14),
                ),
                const SizedBox(height: 8),
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                  children: [
                    ElevatedButton(
                      onPressed: _testDepositWebSocket,
                      style: ElevatedButton.styleFrom(backgroundColor: Colors.orange),
                      child: const Text("Test Deposit"),
                    ),
                    ElevatedButton(
                      onPressed: _testQRScan,
                      style: ElevatedButton.styleFrom(backgroundColor: Colors.purple),
                      child: const Text("Simulate QR"),
                    ),
                  ],
                ),
                const Divider(thickness: 2),
              ],
            ),
          ),

          // PHONE LIST
          Expanded(
            child: _loading
                ? const Center(child: CircularProgressIndicator())
                : _phones.isEmpty
                ? const Center(child: Text("No phones found"))
                : ListView.builder(
              itemCount: _phones.length,
              itemBuilder: (context, index) => _buildPhoneCard(_phones[index]),
            ),
          ),
        ],
      ),
    );
  }

  @override
  void dispose() {
    _socketService.clearOperationCallbacks();
    super.dispose();
  }
}