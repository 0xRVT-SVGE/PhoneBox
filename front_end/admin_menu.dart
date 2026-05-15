import 'dart:async';
import 'dart:convert';          // base64Decode for freeze-frame
import 'dart:typed_data';       // Uint8List
import 'package:flutter/material.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:phonebox_ui/webrtc_config.dart';
import 'api_service.dart';
import 'socket_service.dart';
import 'scan_success_page.dart';

import 'shimmer_widgets.dart'; // Opt #31
import 'report_page.dart';    // ← real ReportPage (was a stub before)

// ══════════════════════════════════════════════════════════
// ADMIN MENU PAGE
// ══════════════════════════════════════════════════════════

class AdminMenuPage extends StatefulWidget {
  const AdminMenuPage({super.key});

  @override
  State<AdminMenuPage> createState() => _AdminMenuPageState();
}

class _AdminMenuPageState extends State<AdminMenuPage> {
  bool _loading = true;
  List<dynamic> _storedPhones  = [];
  List<dynamic> _takenPhones   = [];

  // Top-camera pre-connection (Opt #27)
  final _topRenderer    = RTCVideoRenderer();
  RTCPeerConnection?    _topPc;
  final _topConnected   = ValueNotifier<bool>(false);
  bool _topConnecting   = false;
  bool _topPageDisposed = false;

  @override
  void initState() {
    super.initState();
    _loadData();
    _topRenderer.initialize().then((_) {
      if (!_topPageDisposed) _preconnectTopCamera();
    });
  }

  @override
  void dispose() {
    _topPageDisposed = true;
    _topPc?.onTrack         = null;
    _topPc?.onConnectionState = null;
    _topPc?.close();
    _topPc = null;
    _topRenderer.srcObject = null;
    _topRenderer.dispose();
    _topConnected.dispose();
    super.dispose();
  }

  // ── Top camera pre-connect (Opt #27) ─────────────────

  Future<void> _preconnectTopCamera() async {
    if (_topConnecting || _topConnected.value || _topPageDisposed) return;
    _topConnecting = true;
    try {
      await ApiService.cancelAdmin();
      _topPc = await createPeerConnection(WebRTCConfig.iceConfig);
      _topPc!.onTrack = (event) {
        if (_topPageDisposed || !mounted) return;
        if (event.streams.isNotEmpty) {
          _topRenderer.srcObject = event.streams[0];
          _topConnected.value    = true;
        }
      };
      _topPc!.onConnectionState = (state) async {
        if (_topPageDisposed) return;
        // Only reconnect on 'failed' — 'disconnected' is transient/recoverable.
        if (state == RTCPeerConnectionState.RTCPeerConnectionStateFailed) {
          _topConnected.value = false;
          _topPc?.onTrack           = null;
          _topPc?.onConnectionState = null;
          await _topPc?.close();
          _topPc = null;
          await Future.delayed(const Duration(seconds: 1));
          if (!_topPageDisposed) {
            _topRenderer.srcObject = null;
            _topConnecting = false;
            _preconnectTopCamera();
          }
        }
      };
      final offer = await _topPc!.createOffer(WebRTCConfig.videoOfferConstraints);
      await _topPc!.setLocalDescription(offer);
      final sdp = await ApiService.sendOffer(offer.sdp!, mode: 'admin',
          maxRetries: 2);
      if (sdp != null && !_topPageDisposed) {
        await _topPc!
            .setRemoteDescription(RTCSessionDescription(sdp, 'answer'));
      }
    } catch (_) {
      // silent — page renders fine without camera
    } finally {
      _topConnecting = false;
    }
  }

  // ── Data loading ──────────────────────────────────────

  Future<void> _loadData() async {
    final stored = await ApiService.getStoredPhones();
    final taken  = await ApiService.getTakenPhones();
    if (!mounted) return;
    setState(() {
      _storedPhones = stored ?? [];
      _takenPhones  = taken  ?? [];
      _loading      = false;
    });
  }

  // ── Navigation helpers ────────────────────────────────

  void _openManageStudents() {
    Navigator.push(context,
        MaterialPageRoute(builder: (_) => const ManageStudentsPage()));
  }

  // FIX: was navigating to a stub 'ActivityReportPage' defined in this file.
  // Now navigates to the real ReportPage from report_page.dart.
  void _openActivityReport() {
    Navigator.push(context,
        MaterialPageRoute(builder: (_) => const ReportPage()));
  }

  // ── Build ─────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Admin'),
        actions: [
          IconButton(icon: const Icon(Icons.refresh), onPressed: _loadData),
        ],
      ),
      body: _loading
      // Opt #31: skeleton while data loads
          ? const AdminPhoneListSkeleton()
          : RefreshIndicator(
        onRefresh: _loadData,
        child: ListView(children: [
          _SectionHeader(
            icon: Icons.inventory_2_outlined,
            label: 'Stored Phones (${_storedPhones.length})',
          ),
          if (_storedPhones.isEmpty)
            const _EmptyRow(text: 'No phones currently stored')
          else
            ..._storedPhones.map((p) => _AdminPhoneCard(
              phone: p as Map<String, dynamic>,
              onRefresh: _loadData,
              topRenderer: _topRenderer,
              topConnectedNotifier: _topConnected,
            )),
          const SizedBox(height: 8),
          _SectionHeader(
            icon: Icons.person_outline,
            label: 'Taken Phones (${_takenPhones.length})',
          ),
          if (_takenPhones.isEmpty)
            const _EmptyRow(text: 'No phones taken')
          else
            ..._takenPhones.map((p) => _AdminPhoneCard(
              phone: p as Map<String, dynamic>,
              onRefresh: _loadData,
              topRenderer: _topRenderer,
              topConnectedNotifier: _topConnected,
            )),
          const SizedBox(height: 24),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            child: Column(children: [
              _AdminMenuButton(
                icon: Icons.people_outline,
                label: 'Manage Students',
                onPressed: _openManageStudents,
              ),
              const SizedBox(height: 10),
              _AdminMenuButton(
                icon: Icons.bar_chart_outlined,
                label: 'Activity Report',
                onPressed: _openActivityReport,
              ),
            ]),
          ),
          const SizedBox(height: 24),
        ]),
      ),
    );
  }
}

// ── Section header ────────────────────────────────────────

class _SectionHeader extends StatelessWidget {
  final IconData icon;
  final String label;
  const _SectionHeader({required this.icon, required this.label});

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.fromLTRB(16, 16, 16, 4),
    child: Row(children: [
      Icon(icon, size: 18, color: Colors.grey),
      const SizedBox(width: 6),
      Text(label,
          style: const TextStyle(
              fontSize: 14,
              fontWeight: FontWeight.w600,
              color: Colors.grey)),
    ]),
  );
}

// ── Empty row ─────────────────────────────────────────────

class _EmptyRow extends StatelessWidget {
  final String text;
  const _EmptyRow({required this.text});

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 10),
    child: Text(text,
        style: const TextStyle(color: Colors.grey, fontSize: 13)),
  );
}

// ── Admin menu button ─────────────────────────────────────

class _AdminMenuButton extends StatelessWidget {
  final IconData icon;
  final String label;
  final VoidCallback onPressed;
  const _AdminMenuButton(
      {required this.icon, required this.label, required this.onPressed});

  @override
  Widget build(BuildContext context) => SizedBox(
    width: double.infinity,
    child: OutlinedButton.icon(
      icon: Icon(icon),
      label: Text(label),
      onPressed: onPressed,
      style: OutlinedButton.styleFrom(
        padding: const EdgeInsets.symmetric(vertical: 14),
        shape:
        RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
      ),
    ),
  );
}

// ── Admin phone card ──────────────────────────────────────

class _AdminPhoneCard extends StatelessWidget {
  final Map<String, dynamic> phone;
  final VoidCallback onRefresh;
  final RTCVideoRenderer topRenderer;
  final ValueNotifier<bool> topConnectedNotifier;

  const _AdminPhoneCard({
    required this.phone,
    required this.onRefresh,
    required this.topRenderer,
    required this.topConnectedNotifier,
  });

  String get _locationLabel {
    final lid = phone['lid'];
    final x   = phone['x'];
    final y   = phone['y'];
    if (lid == null && x == null) return 'N/A';
    final slot = lid != null ? (lid as num).toInt() + 1 : null;
    if (slot != null && x != null && y != null)
      return 'slot $slot (row $x, col $y)';
    if (slot != null) return 'slot $slot';
    return 'N/A';
  }

  void _startOperation(BuildContext ctx, bool isDeposit) {
    final pid = phone['pid'].toString();
    final ss  = SocketService();
    if (isDeposit) {
      ss.deposit(pid);
    } else {
      ss.withdraw(pid);
    }
    showModalBottomSheet(
      context: ctx,
      isDismissible: false,
      enableDrag: false,
      isScrollControlled: true,
      backgroundColor: Colors.transparent,
      builder: (_) => DVWBottomSheet(
        pid: pid,
        isDeposit: isDeposit,
        socketService: ss,
        onComplete: onRefresh,
        sharedTopRenderer: topRenderer,
        topConnectedNotifier: topConnectedNotifier,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final isStored    = phone['is_stored'] == true;
    final model       = phone['model']      as String? ?? 'Unknown';
    final sid         = phone['sid']        as String? ?? '—';
    final firstName   = phone['first_name'] as String? ?? '';
    final lastName    = phone['last_name']  as String? ?? '';
    final studentName = '$firstName $lastName'.trim();

    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 5),
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 10, horizontal: 14),
        child: Row(children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(model,
                    style: const TextStyle(
                        fontSize: 15, fontWeight: FontWeight.bold)),
                const SizedBox(height: 3),
                Text('Student: $studentName ($sid)',
                    style: const TextStyle(fontSize: 12, color: Colors.grey)),
                Text('Location: $_locationLabel'),
                Text(
                  isStored ? '📦 Stored' : '🎒 With student',
                  style: TextStyle(
                      color: isStored ? Colors.green[700] : Colors.blue[700],
                      fontWeight: FontWeight.w600,
                      fontSize: 12),
                ),
              ],
            ),
          ),
          Column(children: [
            _SmallBtn(
              label: 'Take',
              color: Colors.green,
              enabled: isStored,
              onPressed: () => _startOperation(context, false),
            ),
            const SizedBox(height: 6),
            _SmallBtn(
              label: 'Put',
              color: Colors.blue,
              enabled: !isStored,
              onPressed: () => _startOperation(context, true),
            ),
          ]),
        ]),
      ),
    );
  }
}

class _SmallBtn extends StatelessWidget {
  final String label;
  final Color color;
  final bool enabled;
  final VoidCallback onPressed;
  const _SmallBtn(
      {required this.label,
        required this.color,
        required this.enabled,
        required this.onPressed});

  @override
  Widget build(BuildContext context) => ElevatedButton(
    onPressed: enabled ? onPressed : null,
    style: ElevatedButton.styleFrom(
        backgroundColor: color,
        fixedSize: const Size(72, 32),
        padding: EdgeInsets.zero,
        textStyle: const TextStyle(fontSize: 12)),
    child: Text(label),
  );
}

// ══════════════════════════════════════════════════════════
// MANAGE STUDENTS PAGE
// ══════════════════════════════════════════════════════════

class ManageStudentsPage extends StatefulWidget {
  const ManageStudentsPage({super.key});

  @override
  State<ManageStudentsPage> createState() => _ManageStudentsPageState();
}

class _ManageStudentsPageState extends State<ManageStudentsPage> {
  bool _loading = true;
  List<dynamic> _students = [];
  final _searchCtrl = TextEditingController();
  Timer? _debounce;

  @override
  void initState() {
    super.initState();
    _loadStudents();
    _searchCtrl.addListener(_onSearch);
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _searchCtrl.dispose();
    super.dispose();
  }

  Future<void> _loadStudents([String query = '']) async {
    if (!mounted) return;
    setState(() => _loading = true);
    final data = query.isEmpty
        ? await ApiService.getStudents()
        : await ApiService.searchStudents(query);
    if (!mounted) return;
    setState(() {
      _students = data ?? [];
      _loading  = false;
    });
  }

  void _onSearch() {
    _debounce?.cancel();
    _debounce = Timer(
      const Duration(milliseconds: 350),
          () => _loadStudents(_searchCtrl.text.trim()),
    );
  }

  Future<void> _delete(String sid) async {
    final ok = await ApiService.deleteStudent(sid);
    if (!mounted) return;
    if (ok) {
      _loadStudents(_searchCtrl.text.trim());
    } else {
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('Failed to delete student')));
    }
  }

  void _openEdit(Map<String, dynamic>? student) {
    Navigator.push(
      context,
      MaterialPageRoute(
          builder: (_) => EditStudentPage(student: student),
          fullscreenDialog: true),
    ).then((_) => _loadStudents(_searchCtrl.text.trim()));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Manage Students'),
        actions: [
          IconButton(
              icon: const Icon(Icons.add),
              onPressed: () => _openEdit(null)),
        ],
      ),
      body: Column(children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 10, 12, 6),
          child: TextField(
            controller: _searchCtrl,
            decoration: InputDecoration(
              hintText: 'Search by name…',
              prefixIcon: const Icon(Icons.search),
              suffixIcon: _searchCtrl.text.isNotEmpty
                  ? IconButton(
                  icon: const Icon(Icons.clear),
                  onPressed: () {
                    _searchCtrl.clear();
                    _loadStudents();
                  })
                  : null,
              border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(10)),
              contentPadding: const EdgeInsets.symmetric(vertical: 10),
            ),
          ),
        ),
        Expanded(
          // Opt #31: StudentListSkeleton while loading
          child: _loading
              ? const StudentListSkeleton()
              : _students.isEmpty
              ? const Center(child: Text('No students found'))
              : ListView.builder(
            itemCount: _students.length,
            itemBuilder: (_, i) {
              final s = _students[i] as Map<String, dynamic>;
              final yearStr = s['year_group'] != null
                  ? '  ·  Year ${s['year_group']}'
                  : '';
              return ListTile(
                title: Text(
                    '${s['first_name']} ${s['last_name']}'),
                subtitle:
                Text('${s['sid'] as String? ?? ''}$yearStr'),
                trailing: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    IconButton(
                      icon: const Icon(Icons.phone_outlined,
                          size: 20),
                      tooltip: 'Phones',
                      onPressed: () => Navigator.push(
                          context,
                          MaterialPageRoute(
                              builder: (_) => StudentPhonesPage(
                                  sid: s['sid'] as String,
                                  studentName:
                                  '${s['first_name']} ${s['last_name']}'))),
                    ),
                    IconButton(
                      icon: const Icon(Icons.edit_outlined,
                          size: 20),
                      tooltip: 'Edit',
                      onPressed: () => _openEdit(s),
                    ),
                    IconButton(
                      icon: const Icon(Icons.delete_outline,
                          size: 20,
                          color: Colors.redAccent),
                      tooltip: 'Delete',
                      onPressed: () => _confirmDelete(
                          context, s['sid'] as String),
                    ),
                  ],
                ),
              );
            },
          ),
        ),
      ]),
    );
  }

  void _confirmDelete(BuildContext ctx, String sid) {
    showDialog(
      context: ctx,
      builder: (_) => AlertDialog(
        title: const Text('Delete student?'),
        content: Text('Remove $sid and all their phones?'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(ctx),
              child: const Text('Cancel')),
          TextButton(
              onPressed: () {
                Navigator.pop(ctx);
                _delete(sid);
              },
              child: const Text('Delete',
                  style: TextStyle(color: Colors.red))),
        ],
      ),
    );
  }
}

// ══════════════════════════════════════════════════════════
// STUDENT PHONES PAGE
// ══════════════════════════════════════════════════════════

class StudentPhonesPage extends StatefulWidget {
  final String sid;
  final String studentName;
  const StudentPhonesPage(
      {super.key, required this.sid, required this.studentName});

  @override
  State<StudentPhonesPage> createState() => _StudentPhonesPageState();
}

class _StudentPhonesPageState extends State<StudentPhonesPage> {
  bool _loading = true;
  List<dynamic> _phones = [];

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    if (!mounted) return;
    setState(() => _loading = true);
    final data = await ApiService.getPhones(widget.sid);
    if (!mounted) return;
    setState(() {
      _phones  = data ?? [];
      _loading = false;
    });
  }

  Future<void> _delete(String pid) async {
    final ok = await ApiService.deletePhone(pid);
    if (!mounted) return;
    if (ok) {
      _load();
    } else {
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('Failed to delete phone')));
    }
  }

  void _openEdit(Map<String, dynamic>? phone) {
    Navigator.push(
      context,
      MaterialPageRoute(
          builder: (_) =>
              EditPhonePage(sid: widget.sid, phone: phone),
          fullscreenDialog: true),
    ).then((_) => _load());
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(widget.studentName),
        actions: [
          IconButton(
              icon: const Icon(Icons.add),
              onPressed: () => _openEdit(null)),
        ],
      ),
      // Opt #31: PhoneListSkeleton while loading
      body: _loading
          ? const PhoneListSkeleton()
          : _phones.isEmpty
          ? const Center(child: Text('No phones registered'))
          : ListView.builder(
        itemCount: _phones.length,
        itemBuilder: (_, i) {
          final p    = _phones[i] as Map<String, dynamic>;
          final pid  = p['pid'].toString();
          final size = (p['phone_size'] as String?) == 'large' ? 'Large' : 'Standard';
          return Card(
            margin: const EdgeInsets.symmetric(
                horizontal: 12, vertical: 5),
            child: ListTile(
              title: Text(p['model'] as String? ?? 'Unknown'),
              subtitle: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('IMEI: ${p['imei'] ?? '—'}'),
                  Text('Cond: ${p['cond'] ?? '—'}  ·  Size: $size'),
                ],
              ),
              isThreeLine: true,
              trailing: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  IconButton(
                    icon: const Icon(Icons.edit_outlined,
                        size: 20),
                    onPressed: () => _openEdit(p),
                  ),
                  IconButton(
                    icon: const Icon(Icons.delete_outline,
                        size: 20, color: Colors.redAccent),
                    onPressed: () =>
                        _confirmDelete(context, pid),
                  ),
                ],
              ),
            ),
          );
        },
      ),
    );
  }

  void _confirmDelete(BuildContext ctx, String pid) {
    showDialog(
      context: ctx,
      builder: (_) => AlertDialog(
        title: const Text('Delete phone?'),
        content: Text('Remove phone $pid?'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(ctx),
              child: const Text('Cancel')),
          TextButton(
              onPressed: () {
                Navigator.pop(ctx);
                _delete(pid);
              },
              child: const Text('Delete',
                  style: TextStyle(color: Colors.red))),
        ],
      ),
    );
  }
}

// ══════════════════════════════════════════════════════════
// EDIT STUDENT PAGE
// ══════════════════════════════════════════════════════════

class EditStudentPage extends StatefulWidget {
  final Map<String, dynamic>? student;
  const EditStudentPage({super.key, this.student});

  @override
  State<EditStudentPage> createState() => _EditStudentPageState();
}

class _EditStudentPageState extends State<EditStudentPage> {
  final _formKey   = GlobalKey<FormState>();
  final _sidCtrl   = TextEditingController();
  final _firstCtrl = TextEditingController();
  final _lastCtrl  = TextEditingController();
  int?    _yearGroup;
  List<double>? _embedding;   // null = not yet captured (create) or unchanged (edit)
  bool _saving = false;

  bool get _isEdit => widget.student != null;

  @override
  void initState() {
    super.initState();
    if (_isEdit) {
      final s = widget.student!;
      _sidCtrl.text   = s['sid']        as String? ?? '';
      _firstCtrl.text = s['first_name'] as String? ?? '';
      _lastCtrl.text  = s['last_name']  as String? ?? '';
      _yearGroup      = s['year_group'] as int?;
    }
  }

  @override
  void dispose() {
    _sidCtrl.dispose();
    _firstCtrl.dispose();
    _lastCtrl.dispose();
    super.dispose();
  }

  Future<void> _captureEmbedding() async {
    final result = await Navigator.push<List<double>>(
      context,
      MaterialPageRoute(
        builder: (_) => const CaptureEmbedPage(),
        fullscreenDialog: true,
      ),
    );
    if (result != null) setState(() => _embedding = result);
  }

  Future<void> _save() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() => _saving = true);

    final payload = <String, dynamic>{
      'sid':        _sidCtrl.text.trim().toUpperCase(),
      'first_name': _firstCtrl.text.trim(),
      'last_name':  _lastCtrl.text.trim(),
      'year_group': _yearGroup,    // null → stored as NULL (unrestricted)
      // 'embed' must be added by the enrolment flow; for admin edits it's
      // omitted so the existing embedding is preserved via COALESCE.
    };

    final bool ok;
    if (_isEdit) {
      final payload = <String, dynamic>{
        'sid':        _sidCtrl.text.trim().toUpperCase(),
        'first_name': _firstCtrl.text.trim(),
        'last_name':  _lastCtrl.text.trim(),
        'year_group': _yearGroup,
      };
      // Only send embed if a new one was captured — otherwise COALESCE keeps old
      if (_embedding != null) payload['embed'] = _embedding;
      ok = await ApiService.updateStudent(
          widget.student!['sid'] as String, payload);
    } else {
      if (_embedding == null) {
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(content: Text('Face capture required to create a student')),
          );
        }
        setState(() => _saving = false);
        return;
      }
      ok = await ApiService.createStudent({
        'sid':        _sidCtrl.text.trim().toUpperCase(),
        'first_name': _firstCtrl.text.trim(),
        'last_name':  _lastCtrl.text.trim(),
        'year_group': _yearGroup,
        'embed':      _embedding,
      });
    }

    if (!mounted) return;
    setState(() => _saving = false);

    if (ok) {
      Navigator.pop(context);
    } else {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(_isEdit ? 'Update failed' : 'Create failed')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(_isEdit ? 'Edit Student' : 'Add Student'),
        actions: [
          if (_saving)
            const Padding(
              padding: EdgeInsets.all(16),
              child: SizedBox(
                width: 20, height: 20,
                child: CircularProgressIndicator(strokeWidth: 2),
              ),
            )
          else
            IconButton(
              icon: const Icon(Icons.check),
              tooltip: 'Save',
              onPressed: _save,
            ),
        ],
      ),
      body: Form(
        key: _formKey,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            // ── SID ───────────────────────────────────────
            TextFormField(
              controller:    _sidCtrl,
              enabled:       !_isEdit,  // SID is the PK — cannot change in edit
              decoration: const InputDecoration(
                labelText:   'Student ID (e.g. E0042)',
                hintText:    'E followed by 4 digits',
                prefixIcon:  Icon(Icons.badge_outlined),
                border:      OutlineInputBorder(),
              ),
              textCapitalization: TextCapitalization.characters,
              validator: (v) {
                if (v == null || v.trim().isEmpty) return 'Required';
                if (!RegExp(r'^E\d{4}$').hasMatch(v.trim().toUpperCase()))
                  return 'Must match E followed by 4 digits (e.g. E0042)';
                return null;
              },
            ),
            const SizedBox(height: 14),

            // ── First name ────────────────────────────────
            TextFormField(
              controller:  _firstCtrl,
              decoration: const InputDecoration(
                labelText:  'First name',
                prefixIcon: Icon(Icons.person_outline),
                border:     OutlineInputBorder(),
              ),
              textCapitalization: TextCapitalization.words,
              validator: (v) =>
                  (v == null || v.trim().isEmpty) ? 'Required' : null,
            ),
            const SizedBox(height: 14),

            // ── Last name ─────────────────────────────────
            TextFormField(
              controller:  _lastCtrl,
              decoration: const InputDecoration(
                labelText:  'Last name',
                prefixIcon: Icon(Icons.person_outline),
                border:     OutlineInputBorder(),
              ),
              textCapitalization: TextCapitalization.words,
              validator: (v) =>
                  (v == null || v.trim().isEmpty) ? 'Required' : null,
            ),
            const SizedBox(height: 14),

            // ── Year group ────────────────────────────────
            DropdownButtonFormField<int?>(
              value:       _yearGroup,
              decoration: const InputDecoration(
                labelText:  'Year group',
                prefixIcon: Icon(Icons.school_outlined),
                border:     OutlineInputBorder(),
                helperText: 'Leave blank for no restriction',
              ),
              items: [
                const DropdownMenuItem<int?>(
                  value: null,
                  child: Text('— Any year (unrestricted) —'),
                ),
                for (int y = 1; y <= 10; y++)
                  DropdownMenuItem<int?>(
                    value: y,
                    child: Text('Year $y'),
                  ),
              ],
              onChanged: (v) => setState(() => _yearGroup = v),
            ),
            const SizedBox(height: 16),

            // ── Face embedding (required for create, optional update) ──
            OutlinedButton.icon(
              icon: Icon(
                _embedding != null ? Icons.check_circle_outline : Icons.face_outlined,
                color: _embedding != null ? Colors.green : null,
              ),
              label: Text(
                _embedding != null
                    ? 'Face captured ✓  (tap to redo)'
                    : _isEdit
                        ? 'Update face embedding (optional)'
                        : 'Capture face embedding  *required*',
                style: TextStyle(
                  color: (!_isEdit && _embedding == null) ? Colors.orange : null,
                ),
              ),
              onPressed: _captureEmbedding,
              style: OutlinedButton.styleFrom(
                padding: const EdgeInsets.symmetric(vertical: 14),
                side: BorderSide(
                  color: _embedding != null
                      ? Colors.green
                      : (!_isEdit ? Colors.orange : Colors.grey),
                ),
              ),
            ),
            const SizedBox(height: 24),

            ElevatedButton.icon(
              icon:    const Icon(Icons.save_outlined),
              label:   Text(_isEdit ? 'Save changes' : 'Create student'),
              onPressed: _saving ? null : _save,
              style: ElevatedButton.styleFrom(
                padding: const EdgeInsets.symmetric(vertical: 14),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ══════════════════════════════════════════════════════════
// EDIT PHONE PAGE
// ══════════════════════════════════════════════════════════

class EditPhonePage extends StatefulWidget {
  final String sid;
  final Map<String, dynamic>? phone;
  const EditPhonePage({super.key, required this.sid, this.phone});

  @override
  State<EditPhonePage> createState() => _EditPhonePageState();
}

class _EditPhonePageState extends State<EditPhonePage> {
  final _formKey    = GlobalKey<FormState>();
  final _modelCtrl  = TextEditingController();
  final _imeiCtrl   = TextEditingController();
  final _condCtrl   = TextEditingController();
  final _adminCtrl  = TextEditingController();
  final _studCtrl   = TextEditingController();

  /// 'standard' or 'large'
  String _phoneSize = 'standard';
  bool _saving = false;

  bool get _isEdit => widget.phone != null;

  static const _conditions = ['Good', 'Fair', 'Damaged', 'Broken'];

  @override
  void initState() {
    super.initState();
    if (_isEdit) {
      final p = widget.phone!;
      _modelCtrl.text = p['model']      as String? ?? '';
      _imeiCtrl.text  = p['imei']       as String? ?? '';
      _condCtrl.text  = p['cond']       as String? ?? '';
      _adminCtrl.text = p['admin_note'] as String? ?? '';
      _studCtrl.text  = p['stud_note']  as String? ?? '';
      _phoneSize      = (p['phone_size'] as String?) == 'large'
                            ? 'large'
                            : 'standard';
    }
  }

  @override
  void dispose() {
    _modelCtrl.dispose();
    _imeiCtrl.dispose();
    _condCtrl.dispose();
    _adminCtrl.dispose();
    _studCtrl.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() => _saving = true);

    final payload = <String, dynamic>{
      'sid':        widget.sid,
      'model':      _modelCtrl.text.trim(),
      'imei':       _imeiCtrl.text.trim(),
      'cond':       _condCtrl.text.trim().isEmpty ? null : _condCtrl.text.trim(),
      'admin_note': _adminCtrl.text.trim().isEmpty ? null : _adminCtrl.text.trim(),
      'stud_note':  _studCtrl.text.trim().isEmpty  ? null : _studCtrl.text.trim(),
      'phone_size': _phoneSize,
    };

    final bool ok;
    if (_isEdit) {
      ok = await ApiService.updatePhone(
          widget.phone!['pid'].toString(), payload);
    } else {
      ok = await ApiService.createPhone(payload);
    }

    if (!mounted) return;
    setState(() => _saving = false);

    if (ok) {
      Navigator.pop(context);
    } else {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(_isEdit ? 'Update failed' : 'Create failed')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(_isEdit ? 'Edit Phone' : 'Add Phone'),
        actions: [
          if (_saving)
            const Padding(
              padding: EdgeInsets.all(16),
              child: SizedBox(
                width: 20, height: 20,
                child: CircularProgressIndicator(strokeWidth: 2),
              ),
            )
          else
            IconButton(
              icon:    const Icon(Icons.check),
              tooltip: 'Save',
              onPressed: _save,
            ),
        ],
      ),
      body: Form(
        key: _formKey,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            // ── Model ─────────────────────────────────────
            TextFormField(
              controller:  _modelCtrl,
              decoration: const InputDecoration(
                labelText:  'Model (e.g. iPhone 15 Pro)',
                prefixIcon: Icon(Icons.phone_iphone_outlined),
                border:     OutlineInputBorder(),
              ),
              validator: (v) =>
                  (v == null || v.trim().isEmpty) ? 'Required' : null,
            ),
            const SizedBox(height: 14),

            // ── IMEI ──────────────────────────────────────
            TextFormField(
              controller:   _imeiCtrl,
              decoration: const InputDecoration(
                labelText:  'IMEI',
                prefixIcon: Icon(Icons.fingerprint),
                border:     OutlineInputBorder(),
              ),
              keyboardType: TextInputType.number,
              validator: (v) =>
                  (v == null || v.trim().isEmpty) ? 'Required' : null,
            ),
            const SizedBox(height: 14),

            // ── Phone size (Standard / Large) ─────────────
            InputDecorator(
              decoration: const InputDecoration(
                labelText: 'Phone size',
                prefixIcon: Icon(Icons.straighten_outlined),
                border: OutlineInputBorder(),
              ),
              child: Row(
                children: [
                  Expanded(
                    child: RadioListTile<String>(
                      title:    const Text('Standard'),
                      value:    'standard',
                      groupValue: _phoneSize,
                      dense:    true,
                      contentPadding: EdgeInsets.zero,
                      onChanged: (v) => setState(() => _phoneSize = v!),
                    ),
                  ),
                  Expanded(
                    child: RadioListTile<String>(
                      title:    const Text('Large'),
                      value:    'large',
                      groupValue: _phoneSize,
                      dense:    true,
                      contentPadding: EdgeInsets.zero,
                      onChanged: (v) => setState(() => _phoneSize = v!),
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 14),

            // ── Condition ─────────────────────────────────
            DropdownButtonFormField<String>(
              value: _conditions.contains(_condCtrl.text)
                  ? _condCtrl.text
                  : null,
              decoration: const InputDecoration(
                labelText:  'Condition',
                prefixIcon: Icon(Icons.health_and_safety_outlined),
                border:     OutlineInputBorder(),
              ),
              items: _conditions
                  .map((c) => DropdownMenuItem(value: c, child: Text(c)))
                  .toList(),
              onChanged: (v) => setState(() => _condCtrl.text = v ?? ''),
            ),
            const SizedBox(height: 14),

            // ── Admin note ────────────────────────────────
            TextFormField(
              controller:  _adminCtrl,
              decoration: const InputDecoration(
                labelText:  'Admin note (optional)',
                prefixIcon: Icon(Icons.admin_panel_settings_outlined),
                border:     OutlineInputBorder(),
              ),
              maxLines: 2,
            ),
            const SizedBox(height: 14),

            // ── Student note ──────────────────────────────
            TextFormField(
              controller:  _studCtrl,
              decoration: const InputDecoration(
                labelText:  'Student note (optional)',
                prefixIcon: Icon(Icons.notes_outlined),
                border:     OutlineInputBorder(),
              ),
              maxLines: 2,
            ),
            const SizedBox(height: 24),

            ElevatedButton.icon(
              icon:    const Icon(Icons.save_outlined),
              label:   Text(_isEdit ? 'Save changes' : 'Register phone'),
              onPressed: _saving ? null : _save,
              style: ElevatedButton.styleFrom(
                padding: const EdgeInsets.symmetric(vertical: 14),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ══════════════════════════════════════════════════════════
// DVWBottomSheet — imported from scan_success_page.dart
// ══════════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════════
// CAPTURE EMBED PAGE  — face embedding capture for enrolment
// Opt: HTTP polling replaces WebRTC (eliminates 1-3s ICE negotiation).
// Two server routes:
//   GET  /api/embed/preview  — JPEG snapshot, ~3 fps poll, no face detection
//   POST /api/embed/capture  — JPEG freeze-frame + embedding vector, one shot
// ══════════════════════════════════════════════════════════

class CaptureEmbedPage extends StatefulWidget {
  const CaptureEmbedPage({super.key});

  @override
  State<CaptureEmbedPage> createState() => _CaptureEmbedPageState();
}

class _CaptureEmbedPageState extends State<CaptureEmbedPage> {
  // Live preview — null until first frame arrives.
  Uint8List?  _previewBytes;
  // Freeze-frame shown after a successful or failed capture.
  Uint8List?  _freezeBytes;

  bool    _capturing = false;   // POST /capture in-flight
  bool    _noCamera  = false;   // server returned 503
  String? _error;               // last error message

  Timer?  _pollTimer;

  @override
  void initState() {
    super.initState();
    // Poll at ~3 fps — fast enough for enrolment, cheap on the server.
    _pollTimer = Timer.periodic(const Duration(milliseconds: 333), (_) => _poll());
    _poll();   // immediate first frame
  }

  @override
  void dispose() {
    _pollTimer?.cancel();
    super.dispose();
  }

  Future<void> _poll() async {
    if (_capturing || !mounted) return;
    final bytes = await ApiService.embedPreview();
    if (!mounted) return;
    if (bytes == null) {
      setState(() => _noCamera = true);
    } else {
      setState(() {
        _previewBytes = bytes;
        _noCamera     = false;
        _freezeBytes  = null;   // clear previous freeze-frame on resume
      });
    }
  }

  Future<void> _capture() async {
    if (_capturing) return;
    setState(() { _capturing = true; _error = null; });
    _pollTimer?.cancel();   // stop refreshing while capture runs

    final data = await ApiService.captureEmbed();
    if (!mounted) return;

    if (data == null) {
      setState(() {
        _error     = 'Network error — check server connection';
        _capturing = false;
      });
      _resumePolling();
      return;
    }

    // Show the server's freeze-frame regardless of outcome.
    final jpegB64 = data['preview_jpeg'] as String?;
    if (jpegB64 != null) {
      setState(() => _freezeBytes = base64Decode(jpegB64));
    }

    if (data['status'] != 'success') {
      setState(() {
        _error     = data['message'] as String? ?? 'Unknown error';
        _capturing = false;
      });
      _resumePolling();
      return;
    }

    // Success — pop back with the embedding vector.
    final rawEmbed = data['embed'] as List;
    final vec      = rawEmbed.map((e) => (e as num).toDouble()).toList();
    if (mounted) Navigator.pop(context, vec);
  }

  void _resumePolling() {
    _pollTimer?.cancel();
    _pollTimer = Timer.periodic(const Duration(milliseconds: 333), (_) => _poll());
  }

  @override
  Widget build(BuildContext context) {
    final displayBytes = _freezeBytes ?? _previewBytes;

    return Scaffold(
      appBar: AppBar(title: const Text('Capture Face Embedding')),
      body: Column(children: [
        Expanded(
          child: _noCamera
              ? const Center(
                  child: Padding(
                    padding: EdgeInsets.all(24),
                    child: Text(
                      'Camera unavailable — ensure the scanner is running.',
                      textAlign: TextAlign.center,
                      style: TextStyle(color: Colors.redAccent),
                    ),
                  ),
                )
              : displayBytes != null
                  ? Image.memory(
                      displayBytes,
                      key: ValueKey(displayBytes.hashCode),
                      fit: BoxFit.contain,
                      gaplessPlayback: true,   // no flicker between polls
                    )
                  : const Center(child: CircularProgressIndicator()),
        ),

        if (_error != null)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
            child: Text(
              _error!,
              textAlign: TextAlign.center,
              style: const TextStyle(color: Colors.redAccent, fontSize: 13),
            ),
          ),

        const Padding(
          padding: EdgeInsets.symmetric(horizontal: 16, vertical: 8),
          child: Text(
            'Centre the student\'s face in frame, then tap "Capture".',
            textAlign: TextAlign.center,
            style: TextStyle(color: Colors.grey, fontSize: 13),
          ),
        ),

        Padding(
          padding: const EdgeInsets.fromLTRB(16, 4, 16, 24),
          child: Row(children: [
            Expanded(
              child: OutlinedButton(
                onPressed: _capturing ? null : () => Navigator.pop(context),
                child: const Text('Cancel'),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: ElevatedButton.icon(
                icon:  _capturing
                    ? const SizedBox(
                        width: 18, height: 18,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Icon(Icons.camera_alt_outlined),
                label: Text(_capturing ? 'Capturing…' : 'Capture'),
                onPressed: _noCamera || _capturing ? null : _capture,
              ),
            ),
          ]),
        ),
      ]),
    );
  }
}
