import 'dart:io';

import 'package:flutter/material.dart';
import 'api_service.dart';

class ScanSuccessPage extends StatefulWidget {
  final String sid;

  const ScanSuccessPage({super.key, required this.sid});

  @override
  State<ScanSuccessPage> createState() => _ScanSuccessPageState();
}

class _ScanSuccessPageState extends State<ScanSuccessPage> {
  bool _loading = true;
  List<dynamic> _phones = [];

  @override
  void initState() {
    super.initState();
    _loadPhones();
  }

  Future<void> _loadPhones() async {
    final data = await ApiService.getPhones(widget.sid);
    setState(() {
      _phones = data ?? [];
      _loading = false;
    });
  }

  Future<void> _takePhone(String pid) async {
    final ok = await ApiService.takePhone(pid);
    if (ok == true && mounted) {
      Navigator.pop(context); // auto exit
    }
  }

  Future<void> _putPhone(String pid) async {
    final ok = await ApiService.putPhone(pid);
    if (ok == true && mounted) {
      Navigator.pop(context); // auto exit
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text("Student ${widget.sid} Phones"),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          onPressed: () => Navigator.pop(context),
        ),
      ),

      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : ListView.builder(
        padding: const EdgeInsets.all(16),
        itemCount: _phones.length,
        itemBuilder: (context, index) {
          final p = _phones[index];

          final id = p["id"];
          final label = p["phone_label"];
          final state = p["state"]; // "in" or "out"

          return Card(
            margin: const EdgeInsets.symmetric(vertical: 8),
            child: ListTile(
              title: Text(label ?? "Phone $id"),
              subtitle: Text("State: $state"),

              trailing: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  // TAKE
                  ElevatedButton(
                    onPressed: state == "in" ? () => _takePhone(id) : null,
                    style: ElevatedButton.styleFrom(
                      backgroundColor: Colors.green,
                    ),
                    child: const Text("Take"),
                  ),
                  const SizedBox(width: 8),

                  // PUT
                  ElevatedButton(
                    onPressed: state == "out" ? () => _putPhone(id) : null,
                    style: ElevatedButton.styleFrom(
                      backgroundColor: Colors.blue,
                    ),
                    child: const Text("Put"),
                  ),
                ],
              ),
            ),
          );
        },
      ),
    );
  }
}
