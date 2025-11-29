import 'package:flutter/material.dart';
import 'api_service.dart';

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

  Future<void> _takePhone(String pid) async {
    final ok = await ApiService.takePhone(pid);
    if (ok == true && mounted) {
      _loadPhones();
    }
  }

  Future<void> _putPhone(String pid) async {
    final ok = await ApiService.putPhone(pid);
    if (ok == true && mounted) {
      _loadPhones();
    }
  }

  Widget _buildPhoneCard(Map<String, dynamic> p) {
    final pid = p["pid"];
    final model = p["model"] ?? "Unknown Model";
    final isStored = p["is_stored"] == true ? "Stored" : "With Student";
    final location = p["location"] != null
        ? "[${p['location'][0]}, ${p['location'][1]}]"
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
                  Text("$model", style: const TextStyle(fontSize: 17, fontWeight: FontWeight.bold)),
                  const SizedBox(height: 4),
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
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _phones.isEmpty
          ? const Center(child: Text("No phones found"))
          : ListView.builder(
        itemCount: _phones.length,
        itemBuilder: (context, index) => _buildPhoneCard(_phones[index]),
      ),
    );
  }
}
