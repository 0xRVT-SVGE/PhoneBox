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
        builder: (context) => ManageStudentsPage(
          students: _students,
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

// ------------------- Manage Students Page -------------------
class ManageStudentsPage extends StatelessWidget {
  final List<dynamic> students;

  const ManageStudentsPage({super.key, required this.students});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text("Manage Students"),
        actions: [
          IconButton(
            icon: const Icon(Icons.add),
            tooltip: "Create New Student",
            onPressed: () {
              // TODO: Implement create student form
              print("Create New Student pressed");
            },
          )
        ],
      ),
      body: students.isEmpty
          ? const Center(child: CircularProgressIndicator())
          : ListView.builder(
        itemCount: students.length,
        itemBuilder: (context, index) {
          final s = students[index];
          return Card(
            margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
            child: ListTile(
              title: Text("${s['first_name']} ${s['last_name']}"),
              subtitle: Text("ID: ${s['sid']}"),
              trailing: Wrap(
                spacing: 6,
                children: [
                  IconButton(
                    icon: const Icon(Icons.edit),
                    tooltip: "Edit Student",
                    onPressed: () {
                      print("Edit ${s['sid']}");
                    },
                  ),
                  IconButton(
                    icon: const Icon(Icons.camera_alt),
                    tooltip: "Update Embed",
                    onPressed: () {
                      print("Update Embed for ${s['sid']}");
                    },
                  ),
                  IconButton(
                    icon: const Icon(Icons.delete),
                    tooltip: "Delete Student",
                    onPressed: () {
                      print("Delete ${s['sid']}");
                    },
                  ),
                  IconButton(
                    icon: const Icon(Icons.phone),
                    tooltip: "View Phones",
                    onPressed: () {
                      print("View phones for ${s['sid']}");
                    },
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
