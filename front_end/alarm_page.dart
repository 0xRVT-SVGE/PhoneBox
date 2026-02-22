import 'package:flutter/material.dart';
import 'socket_service.dart';

class AlarmPage extends StatefulWidget {
  final List<dynamic> initialMismatches;

  const AlarmPage({super.key, required this.initialMismatches});

  @override
  State<AlarmPage> createState() => _AlarmPageState();
}

class _AlarmPageState extends State<AlarmPage> {
  final _socketService = SocketService();
  final _passwordController = TextEditingController();

  List<dynamic> _mismatches = [];
  bool _cleared = false;
  bool _loading = false;
  String? _errorMessage;

  @override
  void initState() {
    super.initState();
    _mismatches = List.from(widget.initialMismatches);

    _socketService.connect(
      onAlarmUpdated: (data) {
        if (!mounted) return;
        setState(() => _mismatches = data["mismatches"] ?? []);
      },
      onAlarmCleared: (_) {
        if (!mounted) return;
        setState(() => _cleared = true);
        Future.delayed(const Duration(seconds: 1), () {
          if (mounted) Navigator.of(context).pop();
        });
      },
      onAlarmAcknowledgeResult: (data) {
        if (!mounted) return;
        setState(() => _loading = false);
        if (data["status"] != "success") {
          setState(() => _errorMessage = "Wrong password");
        }
        // On success, alarm_cleared event will fire and pop this page
      },
    );
  }

  void _submitPassword() {
    final password = _passwordController.text.trim();
    if (password.isEmpty) return;
    setState(() {
      _loading = true;
      _errorMessage = null;
    });
    _socketService.acknowledgeAlarm(password);
  }

  @override
  void dispose() {
    _passwordController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      // Prevent back-swipe dismissal — admin must authenticate
      canPop: _cleared,
      child: Scaffold(
        backgroundColor: Colors.red[900],
        body: SafeArea(
          child: Center(
            child: Card(
              margin: const EdgeInsets.all(24),
              child: Padding(
                padding: const EdgeInsets.all(24),
                child: _cleared
                    ? const Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(Icons.check_circle, color: Colors.green, size: 64),
                    SizedBox(height: 16),
                    Text("Alarm Cleared", style: TextStyle(fontSize: 22)),
                  ],
                )
                    : Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const Icon(Icons.warning_amber_rounded,
                        color: Colors.red, size: 64),
                    const SizedBox(height: 12),
                    const Text(
                      "ALARM ACTIVE",
                      style: TextStyle(
                          fontSize: 26,
                          fontWeight: FontWeight.bold,
                          color: Colors.red),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      "${_mismatches.length} mismatch(es) detected",
                      style: const TextStyle(fontSize: 16),
                    ),
                    const SizedBox(height: 8),
                    if (_mismatches.isNotEmpty)
                      ConstrainedBox(
                        constraints: const BoxConstraints(maxHeight: 160),
                        child: ListView.builder(
                          shrinkWrap: true,
                          itemCount: _mismatches.length,
                          itemBuilder: (_, i) {
                            final m = _mismatches[i];
                            return ListTile(
                              dense: true,
                              leading: const Icon(Icons.smartphone,
                                  color: Colors.orange),
                              title: Text("PID: ${m[0]}"),
                              subtitle: Text("Slot: ${m[1]}"),
                            );
                          },
                        ),
                      ),
                    const Divider(height: 32),
                    const Text(
                      "Admin authentication required to clear",
                      style: TextStyle(fontSize: 14),
                    ),
                    const SizedBox(height: 12),
                    TextField(
                      controller: _passwordController,
                      obscureText: true,
                      decoration: InputDecoration(
                        labelText: "Admin Password",
                        errorText: _errorMessage,
                        border: const OutlineInputBorder(),
                      ),
                      onSubmitted: (_) => _submitPassword(),
                    ),
                    const SizedBox(height: 16),
                    SizedBox(
                      width: double.infinity,
                      child: ElevatedButton(
                        onPressed: _loading ? null : _submitPassword,
                        style: ElevatedButton.styleFrom(
                          backgroundColor: Colors.red,
                          padding: const EdgeInsets.symmetric(vertical: 14),
                        ),
                        child: _loading
                            ? const SizedBox(
                          height: 20,
                          width: 20,
                          child: CircularProgressIndicator(
                              strokeWidth: 2, color: Colors.white),
                        )
                            : const Text("Clear Alarm",
                            style: TextStyle(fontSize: 16)),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}