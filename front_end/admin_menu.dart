import 'package:flutter/material.dart';
import 'auth.dart';
import 'package:phonebox_ui/api_service.dart';



class AdminMenu extends StatefulWidget {
  const AdminMenu({super.key});

  @override
  State<AdminMenu> createState() => _AdminMenuState();
}

class _AdminMenuState extends State<AdminMenu> {
  bool _loading = false;
  List<dynamic> _students = [];

  Future<void> loadStudents() async {
    setState(() => _loading = true);
    final data = await ApiService.getStudents();
    setState(() {
      _students = data ?? [];
      _loading = false;
    });
  }

  void _navigateToStudentManagement() async {
    await loadStudents();
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (context) => Scaffold(
          appBar: AppBar(title: const Text("Manage Students")),
          body: _loading
              ? const Center(child: CircularProgressIndicator())
              : ListView.builder(
            itemCount: _students.length,
            itemBuilder: (context, index) {
              final s = _students[index];
              return ListTile(
                title: Text("${s['first_name']} ${s['last_name']}"),
                subtitle: Text("ID: ${s['id']}"),
              );
            },
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final auth = AuthService();

    return Scaffold(
      appBar: AppBar(
        title: const Text("Admin Menu"),
        actions: [
          IconButton(
            icon: const Icon(Icons.logout),
            onPressed: () {
              auth.logout();
              Navigator.pop(context);
            },
          )
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          ElevatedButton(
            onPressed: _navigateToStudentManagement,
            child: const Text("Manage Students"),
          ),
          ElevatedButton(
            onPressed: () {}, // add other operations here
            child: const Text("Manage Phones"),
          ),
        ],
      ),
    );
  }
}
