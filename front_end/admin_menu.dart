import 'package:flutter/material.dart';
import 'auth.dart';
import 'package:phonebox_ui/api_service.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';


// ══════════════════════════════════════════════════════════
// ADMIN MENU
// ══════════════════════════════════════════════════════════

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
      _loading  = false;
    });
  }

  void _navigateToStudentManagement() async {
    await loadStudents();
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (_) => ManageStudentsPage(initialStudents: _students),
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
          ),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          ElevatedButton(
            onPressed: _navigateToStudentManagement,
            child: const Text("Manage Students"),
          ),
        ],
      ),
    );
  }
}


// ══════════════════════════════════════════════════════════
// MANAGE STUDENTS PAGE
// ══════════════════════════════════════════════════════════

class ManageStudentsPage extends StatefulWidget {
  final List<dynamic> initialStudents;
  const ManageStudentsPage({super.key, required this.initialStudents});

  @override
  State<ManageStudentsPage> createState() => _ManageStudentsPageState();
}

class _ManageStudentsPageState extends State<ManageStudentsPage> {
  List<dynamic> _students = [];
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _loadStudents();
  }

  Future<void> _loadStudents() async {
    setState(() => _loading = true);
    final data = await ApiService.getStudents();
    setState(() {
      _students = data ?? widget.initialStudents;
      _loading  = false;
    });
  }

  Future<void> _deleteStudent(String sid) async {
    final ok = await ApiService.deleteStudent(sid);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(ok ? "Student $sid deleted" : "Failed to delete $sid"),
    ));
    if (ok) _loadStudents();
  }

  Future<void> _updateEmbed(String sid) async {
    final newEmbed = await Navigator.push(
      context,
      MaterialPageRoute(builder: (_) => const CaptureEmbedPage()),
    );
    if (newEmbed == null || newEmbed is! String) return;

    final ok = await ApiService.updateStudent(sid, {"embed": newEmbed});
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(ok ? "Embed updated successfully" : "Failed to update embed"),
    ));
    if (ok) _loadStudents();
  }

  void _showSearchDialog() {
    final formKey  = GlobalKey<FormState>();
    final ctrl     = TextEditingController();

    showDialog(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text("Search Students"),
        content: Form(
          key: formKey,
          child: TextFormField(
            controller: ctrl,
            autofocus: true,
            decoration: const InputDecoration(
                hintText: "Enter Student ID (E0000) or Name"),
            validator: (v) {
              if (v == null || v.trim().isEmpty) return "Please enter ID or name";
              if (!RegExp(r'^E\d{4}$').hasMatch(v.trim()) &&
                  !RegExp(r'^[A-Za-z\s]+$').hasMatch(v.trim())) {
                return "Invalid ID or name format";
              }
              return null;
            },
            onFieldSubmitted: (v) {
              if (formKey.currentState!.validate()) {
                Navigator.pop(context);
                _searchStudents(ctrl.text.trim());
              }
            },
          ),
        ),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text("Cancel")),
          ElevatedButton(
            onPressed: () {
              if (formKey.currentState!.validate()) {
                Navigator.pop(context);
                _searchStudents(ctrl.text.trim());
              }
            },
            child: const Text("Search"),
          ),
        ],
      ),
    );
  }

  Future<void> _searchStudents(String query) async {
    setState(() => _loading = true);
    try {
      final results = await ApiService.searchStudents(query);
      if (!mounted) return;
      setState(() { _students = results; _loading = false; });
    } catch (_) {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text("Manage Students"),
        actions: [
          IconButton(
            icon: const Icon(Icons.search),
            tooltip: "Search Students",
            onPressed: _showSearchDialog,
          ),
          IconButton(
            icon: const Icon(Icons.add),
            tooltip: "Create New Student",
            onPressed: () async {
              await Navigator.push(
                context,
                MaterialPageRoute(builder: (_) => const CreateStudentPage()),
              );
              _loadStudents();
            },
          ),
        ],
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : ListView.builder(
        itemCount: _students.length,
        itemBuilder: (_, index) {
          final s = _students[index];
          return Card(
            margin: const EdgeInsets.symmetric(
                horizontal: 12, vertical: 6),
            child: ListTile(
              title: Text("${s['first_name']} ${s['last_name']}"),
              subtitle: Text("ID: ${s['sid']}"),
              trailing: Wrap(
                spacing: 6,
                children: [
                  IconButton(
                    icon: const Icon(Icons.edit),
                    tooltip: "Edit Student",
                    onPressed: () async {
                      final updated = await Navigator.push(
                        context,
                        MaterialPageRoute(
                            builder: (_) =>
                                EditStudentPage(student: s)),
                      );
                      if (updated == true) _loadStudents();
                    },
                  ),
                  IconButton(
                    icon: const Icon(Icons.camera_alt),
                    tooltip: "Update Embed",
                    onPressed: () => _updateEmbed(s['sid']),
                  ),
                  IconButton(
                    icon: const Icon(Icons.delete),
                    tooltip: "Delete Student",
                    onPressed: () => _deleteStudent(s['sid']),
                  ),
                  IconButton(
                    icon: const Icon(Icons.phone),
                    tooltip: "View Phones",
                    onPressed: () => Navigator.push(
                      context,
                      MaterialPageRoute(
                        builder: (_) =>
                            StudentPhonesPage(studentId: s['sid']),
                      ),
                    ),
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


// ══════════════════════════════════════════════════════════
// STUDENT PHONES PAGE
// ══════════════════════════════════════════════════════════

class StudentPhonesPage extends StatefulWidget {
  final String studentId;
  const StudentPhonesPage({super.key, required this.studentId});

  @override
  State<StudentPhonesPage> createState() => _StudentPhonesPageState();
}

class _StudentPhonesPageState extends State<StudentPhonesPage> {
  List<dynamic> _phones = [];
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _loadPhones();
  }

  Future<void> _loadPhones() async {
    setState(() => _loading = true);
    final data = await ApiService.getPhones(widget.studentId);
    setState(() {
      _phones  = data ?? [];
      _loading = false;
    });
  }

  Future<void> _deletePhone(String pid) async {
    final ok = await ApiService.deletePhone(pid);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(ok ? "Phone $pid deleted" : "Failed to delete phone $pid"),
    ));
    if (ok) _loadPhones();
  }

  Future<void> _editPhone(Map<String, dynamic> phone) async {
    final updated = await Navigator.push(
      context,
      MaterialPageRoute(builder: (_) => EditPhonePage(phone: phone)),
    );
    if (updated == true) _loadPhones();
  }

  Future<void> _addPhone() async {
    final created = await Navigator.push(
      context,
      MaterialPageRoute(
          builder: (_) => CreatePhonePage(studentId: widget.studentId)),
    );
    if (created == true) _loadPhones();
  }

  // ── Location helper ───────────────────────────────────

  /// "slot N (row R, col C)" — lid is 0-based; x = row, y = col.
  static String _locationLabel(Map<String, dynamic> p) {
    final lid = p['lid'];
    final x   = p['x'];
    final y   = p['y'];

    if (lid == null && x == null) return 'N/A';

    final slotNum = lid != null ? (lid as num).toInt() + 1 : null;

    if (slotNum != null && x != null && y != null) {
      return 'slot $slotNum (row $x, col $y)';
    }
    if (slotNum != null) return 'slot $slotNum';
    if (x != null && y != null) return 'row $x, col $y';
    return 'N/A';
  }

  // ── Phone card ────────────────────────────────────────

  Widget _buildPhoneCard(Map<String, dynamic> p) {
    final pid      = p["pid"].toString();
    final model    = p["model"] ?? "Unknown Model";
    final stored   = p["is_stored"] == true;
    final location = _locationLabel(p);

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
                  Text(model,
                      style: const TextStyle(
                          fontSize: 17, fontWeight: FontWeight.bold)),
                  const SizedBox(height: 4),
                  Text("PID: $pid"),
                  if (p['imei']       != null) Text("IMEI: ${p['imei']}"),
                  if (p['cond']       != null) Text("Condition: ${p['cond']}"),
                  if (p['admin_note'] != null) Text("Admin note: ${p['admin_note']}"),
                  if (p['stud_note']  != null) Text("Student note: ${p['stud_note']}"),
                  Text("Location: $location"),
                  Text(stored ? "📦 Stored" : "🎒 With student"),
                ],
              ),
            ),
            Column(
              children: [
                ElevatedButton(
                  onPressed: stored ? () {} : null,
                  style: ElevatedButton.styleFrom(
                      backgroundColor: Colors.green,
                      fixedSize: const Size(80, 34)),
                  child: const Text("Take"),
                ),
                const SizedBox(height: 8),
                ElevatedButton(
                  onPressed: !stored ? () {} : null,
                  style: ElevatedButton.styleFrom(
                      backgroundColor: Colors.blue,
                      fixedSize: const Size(80, 34)),
                  child: const Text("Put"),
                ),
                const SizedBox(height: 8),
                Wrap(
                  children: [
                    IconButton(
                      icon: const Icon(Icons.edit),
                      tooltip: "Edit Phone",
                      onPressed: () => _editPhone(p),
                    ),
                    IconButton(
                      icon: const Icon(Icons.delete),
                      tooltip: "Delete Phone",
                      onPressed: () => _deletePhone(pid),
                    ),
                  ],
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
        title: Text("Phones — ${widget.studentId}"),
        actions: [
          IconButton(
            icon: const Icon(Icons.add),
            tooltip: "Add Phone",
            onPressed: _addPhone,
          ),
        ],
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


// ══════════════════════════════════════════════════════════
// CREATE STUDENT PAGE
// ══════════════════════════════════════════════════════════

class CreateStudentPage extends StatefulWidget {
  const CreateStudentPage({super.key});

  @override
  State<CreateStudentPage> createState() => _CreateStudentPageState();
}

class _CreateStudentPageState extends State<CreateStudentPage> {
  final _formKey    = GlobalKey<FormState>();
  final _sid        = TextEditingController();
  final _firstName  = TextEditingController();
  final _lastName   = TextEditingController();
  String? _embedding;
  bool _loading = false;

  Future<void> _openPreview() async {
    final result = await Navigator.push(
      context,
      MaterialPageRoute(builder: (_) => const CaptureEmbedPage()),
    );
    if (result != null && result is String) {
      setState(() => _embedding = result);
    }
  }

  Future<void> _submit() async {
    if (_embedding == null) {
      ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text("Embedding required")));
      return;
    }
    if (!_formKey.currentState!.validate()) return;

    setState(() => _loading = true);
    final ok = await ApiService.createStudent({
      "sid":        _sid.text.trim(),
      "first_name": _firstName.text.trim(),
      "last_name":  _lastName.text.trim().isEmpty ? null : _lastName.text.trim(),
      "embed":      _embedding,
    });
    setState(() => _loading = false);
    if (!mounted) return;

    if (ok == true) {
      Navigator.pop(context);
    } else {
      ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text("Failed to create student")));
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
                decoration: const InputDecoration(
                    labelText: "Student ID (E0000)"),
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
              Text(
                _embedding == null
                    ? "No embedding calculated"
                    : "Embedding ready ✓",
                style: TextStyle(
                    color: _embedding == null ? Colors.red : Colors.green,
                    fontWeight: FontWeight.bold),
              ),
              const SizedBox(height: 8),
              ElevatedButton(
                onPressed: _openPreview,
                child: const Text("Calculate Embedding"),
              ),
              const SizedBox(height: 24),
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


// ══════════════════════════════════════════════════════════
// CAPTURE EMBED PAGE
// ══════════════════════════════════════════════════════════

class CaptureEmbedPage extends StatefulWidget {
  const CaptureEmbedPage({super.key});

  @override
  State<CaptureEmbedPage> createState() => _CaptureEmbedPageState();
}

class _CaptureEmbedPageState extends State<CaptureEmbedPage> {
  bool _loading = true;
  String? _error;

  final RTCVideoRenderer _renderer = RTCVideoRenderer();
  RTCPeerConnection? _pc;

  @override
  void initState() {
    super.initState();
    _initPreview();
  }

  Future<void> _initPreview() async {
    await _renderer.initialize();

    _pc = await createPeerConnection({
      'iceServers': [
        {'urls': 'stun:stun.l.google.com:19302'},
      ]
    });

    _pc!.onTrack = (event) {
      if (event.streams.isNotEmpty) {
        _renderer.srcObject = event.streams[0];
      }
    };

    final offer = await _pc!.createOffer({
      'offerToReceiveVideo': true,
      'offerToReceiveAudio': false,
    });
    await _pc!.setLocalDescription(offer);

    final answerSDP = await ApiService.createPreviewOffer(offer.sdp!);
    if (!mounted) return;

    if (answerSDP != null) {
      await _pc!.setRemoteDescription(
          RTCSessionDescription(answerSDP, 'answer'));
      setState(() { _loading = false; _error = null; });
    } else {
      await _pc?.close();
      _pc = null;
      setState(() {
        _error   = "Failed to initialize preview";
        _loading = false;
      });
    }
  }

  Future<void> _takePhoto() async {
    final data = await ApiService.takePhoto();
    if (!mounted) return;
    if (data == null || data["embed"] == null) {
      ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text("Failed to capture photo")));
      return;
    }
    Navigator.pop(context, data["embed"]);
  }

  @override
  void dispose() {
    _pc?.onTrack = null;
    _pc?.close();
    _pc = null;
    ApiService.cancelPreview();
    _renderer.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Scaffold(
          body: Center(child: CircularProgressIndicator()));
    }
    return Scaffold(
      appBar: AppBar(title: const Text("Capture Embedding")),
      body: Column(
        children: [
          Expanded(
            child: _error != null
                ? Center(child: Text(_error!))
                : RTCVideoView(_renderer),
          ),
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceEvenly,
            children: [
              ElevatedButton(
                onPressed: () async {
                  await _pc?.close();
                  _pc = null;
                  await ApiService.cancelPreview();
                  if (mounted) Navigator.pop(context);
                },
                child: const Text("Cancel"),
              ),
              ElevatedButton(
                onPressed: _takePhoto,
                child: const Text("Take Photo"),
              ),
            ],
          ),
          const SizedBox(height: 12),
        ],
      ),
    );
  }
}


// ══════════════════════════════════════════════════════════
// EDIT STUDENT PAGE
// ══════════════════════════════════════════════════════════

class EditStudentPage extends StatefulWidget {
  final Map<String, dynamic> student;
  const EditStudentPage({super.key, required this.student});

  @override
  State<EditStudentPage> createState() => _EditStudentPageState();
}

class _EditStudentPageState extends State<EditStudentPage> {
  final _formKey   = GlobalKey<FormState>();
  final _sid       = TextEditingController();
  final _firstName = TextEditingController();
  final _lastName  = TextEditingController();
  String? _embedding;
  bool _loading = false;

  Future<void> _openPreview() async {
    final result = await Navigator.push(
      context,
      MaterialPageRoute(builder: (_) => const CaptureEmbedPage()),
    );
    if (result != null && result is String) {
      setState(() => _embedding = result);
    }
  }

  Future<void> _submit() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() => _loading = true);

    final payload = <String, dynamic>{};
    if (_sid.text.trim().isNotEmpty)        payload['sid']        = _sid.text.trim();
    if (_firstName.text.trim().isNotEmpty)  payload['first_name'] = _firstName.text.trim();
    if (_lastName.text.trim().isNotEmpty)   payload['last_name']  = _lastName.text.trim();
    if (_embedding != null)                 payload['embed']      = _embedding;

    final ok = await ApiService.updateStudent(widget.student['sid'], payload);
    setState(() => _loading = false);
    if (!mounted) return;

    if (ok) {
      Navigator.pop(context, true);
    } else {
      ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text("Failed to update student")));
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text("Edit Student")),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Form(
          key: _formKey,
          child: ListView(
            children: [
              TextFormField(
                controller: _sid,
                decoration: InputDecoration(
                  labelText: "Student ID (E0000)",
                  hintText:
                  "${widget.student['sid']} (leave empty = no change)",
                ),
                validator: (v) {
                  if (v != null &&
                      v.isNotEmpty &&
                      !RegExp(r"^E\d{4}$").hasMatch(v.trim())) {
                    return "Invalid ID format";
                  }
                  return null;
                },
              ),
              const SizedBox(height: 12),
              TextFormField(
                controller: _firstName,
                decoration: InputDecoration(
                  labelText: "First Name",
                  hintText:
                  "${widget.student['first_name']} (leave empty = no change)",
                ),
              ),
              const SizedBox(height: 12),
              TextFormField(
                controller: _lastName,
                decoration: InputDecoration(
                  labelText: "Last Name",
                  hintText:
                  "${widget.student['last_name']} (leave empty = no change)",
                ),
              ),
              const SizedBox(height: 12),
              if (_embedding != null)
                Text("New embedding ready ✓",
                    style: const TextStyle(
                        color: Colors.green, fontWeight: FontWeight.bold)),
              const SizedBox(height: 8),
              ElevatedButton(
                onPressed: _openPreview,
                child: const Text("Update Embedding"),
              ),
              const SizedBox(height: 24),
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                children: [
                  ElevatedButton(
                    onPressed: () => Navigator.pop(context),
                    child: const Text("Cancel"),
                  ),
                  ElevatedButton(
                    onPressed: _loading ? null : _submit,
                    child: _loading
                        ? const CircularProgressIndicator()
                        : const Text("Save Changes"),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}


// ══════════════════════════════════════════════════════════
// CREATE PHONE PAGE
// ══════════════════════════════════════════════════════════

class CreatePhonePage extends StatefulWidget {
  final String studentId;
  const CreatePhonePage({super.key, required this.studentId});

  @override
  State<CreatePhonePage> createState() => _CreatePhonePageState();
}

class _CreatePhonePageState extends State<CreatePhonePage> {
  final _formKey   = GlobalKey<FormState>();
  final _model     = TextEditingController();
  final _imei      = TextEditingController();
  final _adminNote = TextEditingController();
  final _studNote  = TextEditingController();
  final _locX      = TextEditingController();
  final _locY      = TextEditingController();
  String? _cond;
  bool _loading = false;

  Future<void> _submit() async {
    if (!_formKey.currentState!.validate()) return;

    final location =
    (_locX.text.isNotEmpty && _locY.text.isNotEmpty)
        ? [int.parse(_locX.text), int.parse(_locY.text)]
        : null;

    setState(() => _loading = true);
    final ok = await ApiService.createPhone({
      "sid":        widget.studentId,
      "model":      _model.text.trim(),
      "imei":       _imei.text.trim(),
      "cond":       _cond,
      "admin_note": _adminNote.text.trim().isEmpty ? null : _adminNote.text.trim(),
      "stud_note":  _studNote.text.trim().isEmpty  ? null : _studNote.text.trim(),
      "location":   location,
    });
    setState(() => _loading = false);

    if (ok) Navigator.pop(context, true);
    else
      ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text("Failed to create phone")));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text("Add Phone")),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Form(
          key: _formKey,
          child: ListView(
            children: [
              TextFormField(
                controller: _model,
                decoration: const InputDecoration(labelText: "Model"),
                validator: (v) =>
                (v == null || v.isEmpty) ? "Required" : null,
              ),
              const SizedBox(height: 12),
              TextFormField(
                controller: _imei,
                decoration: const InputDecoration(labelText: "IMEI"),
                validator: (v) =>
                (v == null || v.isEmpty) ? "Required" : null,
              ),
              const SizedBox(height: 12),
              DropdownButtonFormField<String>(
                value: _cond,
                items: ['New', 'Good', 'Fair', 'Damaged', 'Broken']
                    .map((e) =>
                    DropdownMenuItem(value: e, child: Text(e)))
                    .toList(),
                decoration: const InputDecoration(labelText: "Condition"),
                onChanged: (v) => setState(() => _cond = v),
                validator: (v) =>
                (v == null || v.isEmpty) ? "Required" : null,
              ),
              const SizedBox(height: 12),
              TextFormField(
                controller: _adminNote,
                decoration: const InputDecoration(
                    labelText: "Admin Note (optional)"),
              ),
              const SizedBox(height: 12),
              TextFormField(
                controller: _studNote,
                decoration: const InputDecoration(
                    labelText: "Student Note (optional)"),
              ),
              const SizedBox(height: 12),
              Row(children: [
                Expanded(
                  child: TextFormField(
                    controller: _locX,
                    decoration:
                    const InputDecoration(labelText: "Row (optional)"),
                    keyboardType: TextInputType.number,
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: TextFormField(
                    controller: _locY,
                    decoration:
                    const InputDecoration(labelText: "Col (optional)"),
                    keyboardType: TextInputType.number,
                  ),
                ),
              ]),
              const SizedBox(height: 24),
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                children: [
                  ElevatedButton(
                    onPressed: () => Navigator.pop(context),
                    child: const Text("Cancel"),
                  ),
                  ElevatedButton(
                    onPressed: _loading ? null : _submit,
                    child: _loading
                        ? const CircularProgressIndicator()
                        : const Text("Add Phone"),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}


// ══════════════════════════════════════════════════════════
// EDIT PHONE PAGE
// ══════════════════════════════════════════════════════════

class EditPhonePage extends StatefulWidget {
  final Map<String, dynamic> phone;
  const EditPhonePage({super.key, required this.phone});

  @override
  State<EditPhonePage> createState() => _EditPhonePageState();
}

class _EditPhonePageState extends State<EditPhonePage> {
  final _formKey   = GlobalKey<FormState>();
  final _model     = TextEditingController();
  final _imei      = TextEditingController();
  final _adminNote = TextEditingController();
  final _studNote  = TextEditingController();
  final _locX      = TextEditingController();
  final _locY      = TextEditingController();
  String? _cond;
  bool _loading = false;

  @override
  void initState() {
    super.initState();
    _model.text     = widget.phone['model']      ?? '';
    _imei.text      = widget.phone['imei']       ?? '';
    _cond           = widget.phone['cond'];
    _adminNote.text = widget.phone['admin_note'] ?? '';
    _studNote.text  = widget.phone['stud_note']  ?? '';
    if (widget.phone['location'] != null) {
      _locX.text = widget.phone['location'][0].toString();
      _locY.text = widget.phone['location'][1].toString();
    }
  }

  Future<void> _submit() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() => _loading = true);

    final payload = <String, dynamic>{};
    if (_model.text.trim().isNotEmpty)     payload['model']      = _model.text.trim();
    if (_imei.text.trim().isNotEmpty)      payload['imei']       = _imei.text.trim();
    if (_cond != null)                     payload['cond']       = _cond;
    if (_adminNote.text.trim().isNotEmpty) payload['admin_note'] = _adminNote.text.trim();
    if (_studNote.text.trim().isNotEmpty)  payload['stud_note']  = _studNote.text.trim();
    if (_locX.text.isNotEmpty && _locY.text.isNotEmpty) {
      payload['location'] = [int.parse(_locX.text), int.parse(_locY.text)];
    }

    final ok = await ApiService.updatePhone(widget.phone['pid'], payload);
    setState(() => _loading = false);
    if (!mounted) return;

    if (ok) Navigator.pop(context, true);
    else
      ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text("Failed to update phone")));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text("Edit Phone")),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Form(
          key: _formKey,
          child: ListView(
            children: [
              TextFormField(
                controller: _model,
                decoration: InputDecoration(
                  labelText: "Model",
                  hintText:
                  "${widget.phone['model']} (leave empty = no change)",
                ),
              ),
              const SizedBox(height: 12),
              TextFormField(
                controller: _imei,
                decoration: InputDecoration(
                  labelText: "IMEI",
                  hintText:
                  "${widget.phone['imei'] ?? ''} (leave empty = no change)",
                ),
              ),
              const SizedBox(height: 12),
              DropdownButtonFormField<String>(
                value: _cond,
                items: ['New', 'Good', 'Fair', 'Damaged', 'Broken']
                    .map((e) =>
                    DropdownMenuItem(value: e, child: Text(e)))
                    .toList(),
                decoration: const InputDecoration(labelText: "Condition"),
                onChanged: (v) => setState(() => _cond = v),
              ),
              const SizedBox(height: 12),
              TextFormField(
                controller: _adminNote,
                decoration: InputDecoration(
                  labelText: "Admin Note",
                  hintText:
                  "${widget.phone['admin_note'] ?? ''} (leave empty = no change)",
                ),
              ),
              const SizedBox(height: 12),
              TextFormField(
                controller: _studNote,
                decoration: InputDecoration(
                  labelText: "Student Note",
                  hintText:
                  "${widget.phone['stud_note'] ?? ''} (leave empty = no change)",
                ),
              ),
              const SizedBox(height: 12),
              Row(children: [
                Expanded(
                  child: TextFormField(
                    controller: _locX,
                    decoration: const InputDecoration(
                        labelText: "Row (optional)"),
                    keyboardType: TextInputType.number,
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: TextFormField(
                    controller: _locY,
                    decoration: const InputDecoration(
                        labelText: "Col (optional)"),
                    keyboardType: TextInputType.number,
                  ),
                ),
              ]),
              const SizedBox(height: 24),
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                children: [
                  ElevatedButton(
                    onPressed: () => Navigator.pop(context),
                    child: const Text("Cancel"),
                  ),
                  ElevatedButton(
                    onPressed: _loading ? null : _submit,
                    child: _loading
                        ? const CircularProgressIndicator()
                        : const Text("Save Changes"),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}