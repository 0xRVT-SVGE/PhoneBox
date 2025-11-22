import 'package:flutter/material.dart';
import 'auth.dart';
import 'package:phonebox_ui/api_service.dart';

// ======================================================================
// ADMIN MENU
// ======================================================================
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
            onPressed: () {},
            child: const Text("Manage Phones"),
          ),
        ],
      ),
    );
  }
}

// ======================================================================
// MANAGE STUDENTS PAGE
// ======================================================================
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
              Navigator.push(
                context,
                MaterialPageRoute(
                  builder: (_) => const CreateStudentPage(),
                ),
              );
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
            margin:
            const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
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

// ======================================================================
// CREATE STUDENT PAGE (FULL FORM)
// ======================================================================
class CreateStudentPage extends StatefulWidget {
  const CreateStudentPage({super.key});

  @override
  State<CreateStudentPage> createState() => _CreateStudentPageState();
}

class _CreateStudentPageState extends State<CreateStudentPage> {
  final _formKey = GlobalKey<FormState>();

  final TextEditingController _sid = TextEditingController();
  final TextEditingController _firstName = TextEditingController();
  final TextEditingController _lastName = TextEditingController();
  final TextEditingController _embed = TextEditingController();
  final TextEditingController _location = TextEditingController();

  bool _loading = false;

  // ----------------------------------------------------------------------
  // FORMAT POSTGRESQL ARRAYS
  // ----------------------------------------------------------------------
  String normalizePgArray(String raw) {
    final t = raw.trim();
    if (t.isEmpty) return "{}";

    if (t.startsWith("{") && t.endsWith("}")) return t;

    return "{$t}";
  }

  Future<void> _submit() async {
    if (!_formKey.currentState!.validate()) return;

    setState(() => _loading = true);

    final payload = {
      "sid": _sid.text.trim(),
      "first_name": _firstName.text.trim(),
      "last_name": _lastName.text.trim().isEmpty
          ? null
          : _lastName.text.trim(),

      "embed": normalizePgArray(_embed.text),
      "location": _location.text.trim().isEmpty
          ? null
          : normalizePgArray(_location.text),
    };

    final ok = await ApiService.createStudent(payload);

    setState(() => _loading = false);

    if (!mounted) return;

    if (ok == true) {
      Navigator.pop(context);
    } else {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text("Failed to create student")),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text("Create Student")),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Form(
          key: _formKey,
          child: ListView(
            children: [
              TextFormField(
                controller: _sid,
                decoration: const InputDecoration(labelText: "Student ID (E0000)"),
                validator: (v) {
                  if (v == null || v.isEmpty) return "Required";
                  if (!RegExp(r"^E\d{4}$").hasMatch(v.trim())) {
                    return "Invalid ID format";
                  }
                  return null;
                },
              ),
              const SizedBox(height: 12),
              TextFormField(
                controller: _firstName,
                decoration: const InputDecoration(labelText: "First Name"),
                validator: (v) =>
                (v == null || v.isEmpty) ? "Required" : null,
              ),
              const SizedBox(height: 12),
              TextFormField(
                controller: _lastName,
                decoration: const InputDecoration(labelText: "Last Name"),
              ),
              const SizedBox(height: 12),

              // ===================== EMBED FIELD =====================
              TextFormField(
                controller: _embed,
                decoration:
                const InputDecoration(labelText: "Embed (e.g. 1.2,2.3,3.4)"),
                validator: (v) {
                  if (v == null || v.isEmpty) return "Required";
                  final t = v.trim();
                  if (t.contains(" ")) return "No spaces allowed";
                  if (t.startsWith("{") && !t.endsWith("}")) return "Unmatched {";
                  if (t.endsWith("}") && !t.startsWith("{")) return "Unmatched }";
                  return null;
                },
              ),
              const SizedBox(height: 12),

              // ===================== LOCATION FIELD =====================
              TextFormField(
                controller: _location,
                decoration:
                const InputDecoration(labelText: "Location (optional)"),
                validator: (v) {
                  if (v == null || v.isEmpty) return null;
                  final t = v.trim();
                  if (t.contains(" ")) return "No spaces allowed";
                  if (t.startsWith("{") && !t.endsWith("}")) return "Unmatched {";
                  if (t.endsWith("}") && !t.startsWith("{")) return "Unmatched }";
                  return null;
                },
              ),
              const SizedBox(height: 20),

              ElevatedButton(
                onPressed: _loading ? null : _submit,
                child: _loading
                    ? const CircularProgressIndicator()
                    : const Text("Create Student"),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
