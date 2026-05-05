import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'api_service.dart';
import 'socket_service.dart';
import 'webrtc_config.dart';
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
        if (state == RTCPeerConnectionState.RTCPeerConnectionStateDisconnected ||
            state == RTCPeerConnectionState.RTCPeerConnectionStateFailed) {
          _topConnected.value = false;
          await _topPc?.close();
          _topPc = null;
          await Future.delayed(const Duration(seconds: 3));
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
              return ListTile(
                title: Text(
                    '${s['first_name']} ${s['last_name']}'),
                subtitle:
                Text(s['sid'] as String? ?? ''),
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
          final p   = _phones[i] as Map<String, dynamic>;
          final pid = p['pid'].toString();
          return Card(
            margin: const EdgeInsets.symmetric(
                horizontal: 12, vertical: 5),
            child: ListTile(
              title: Text(p['model'] as String? ?? 'Unknown'),
              subtitle: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('IMEI: ${p['imei'] ?? '—'}'),
                  Text('Cond: ${p['cond'] ?? '—'}'),
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
// EDIT STUDENT PAGE  (stub — replace with your form)
// ══════════════════════════════════════════════════════════

class EditStudentPage extends StatelessWidget {
  final Map<String, dynamic>? student;
  const EditStudentPage({super.key, this.student});

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
        title: Text(student == null ? 'Add Student' : 'Edit Student')),
    body: const Center(child: Text('Student form goes here')),
  );
}

// ══════════════════════════════════════════════════════════
// EDIT PHONE PAGE  (stub — replace with your form)
// ══════════════════════════════════════════════════════════

class EditPhonePage extends StatelessWidget {
  final String sid;
  final Map<String, dynamic>? phone;
  const EditPhonePage({super.key, required this.sid, this.phone});

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar:
    AppBar(title: Text(phone == null ? 'Add Phone' : 'Edit Phone')),
    body: const Center(child: Text('Phone form goes here')),
  );
}

// ══════════════════════════════════════════════════════════
// DVWBottomSheet — imported from scan_success_page.dart
// ══════════════════════════════════════════════════════════