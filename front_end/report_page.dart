// report_page.dart
//
// Required packages (add to pubspec.yaml):
//   pdf: ^3.10.8
//   printing: ^5.12.0
//   intl: ^0.19.0   (likely already present)
//
// Usage: Navigator.push(context, MaterialPageRoute(builder: (_) => const ReportPage()));

import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:intl/intl.dart';
import 'package:pdf/pdf.dart';
import 'package:pdf/widgets.dart' as pw;
import 'package:printing/printing.dart';

const String _baseUrl = "http://localhost:5000";

// ── Date/time formatting helpers ─────────────────────────
final _dtFmt  = DateFormat("dd/MM/yyyy HH:mm");
final _hdrFmt = DateFormat("dd/MM/yyyy HH:mm:ss");

String _fmt(String? iso) {
  if (iso == null) return '—';
  try {
    return _dtFmt.format(DateTime.parse(iso).toLocal());
  } catch (_) {
    return iso;
  }
}

// ══════════════════════════════════════════════════════════
// REPORT PAGE
// ══════════════════════════════════════════════════════════

class ReportPage extends StatefulWidget {
  const ReportPage({super.key});

  @override
  State<ReportPage> createState() => _ReportPageState();
}

class _ReportPageState extends State<ReportPage> {
  // ── Interval ──────────────────────────────────────────
  DateTime _fromDate = DateTime.now().subtract(const Duration(days: 7));
  DateTime _toDate   = DateTime.now();
  TimeOfDay _fromTime = TimeOfDay.zero;
  TimeOfDay _toTime   = const TimeOfDay(hour: 23, minute: 59);

  // ── Stress hours (optional — operations outside these hours get highlighted)
  bool       _stressEnabled = false;
  TimeOfDay  _stressStart   = const TimeOfDay(hour: 8,  minute: 0);
  TimeOfDay  _stressEnd     = const TimeOfDay(hour: 18, minute: 0);

  bool    _loading = false;
  String? _errorMsg;

  // ── Computed datetimes ────────────────────────────────

  DateTime get _fromDt => DateTime(
      _fromDate.year, _fromDate.month, _fromDate.day,
      _fromTime.hour, _fromTime.minute);

  DateTime get _toDt => DateTime(
      _toDate.year, _toDate.month, _toDate.day,
      _toTime.hour, _toTime.minute);

  // ── Date pickers ──────────────────────────────────────

  Future<void> _pickDate(bool isFrom) async {
    final initial = isFrom ? _fromDate : _toDate;
    final picked  = await showDatePicker(
      context: context,
      initialDate: initial,
      firstDate: DateTime(2020),
      lastDate: DateTime.now().add(const Duration(days: 1)),
    );
    if (picked == null || !mounted) return;
    setState(() {
      if (isFrom) _fromDate = picked;
      else        _toDate   = picked;
    });
  }

  Future<void> _pickTime(bool isFrom) async {
    final initial = isFrom ? _fromTime : _toTime;
    final picked  = await showTimePicker(context: context, initialTime: initial);
    if (picked == null || !mounted) return;
    setState(() {
      if (isFrom) _fromTime = picked;
      else        _toTime   = picked;
    });
  }

  Future<void> _pickStressTime(bool isStart) async {
    final initial = isStart ? _stressStart : _stressEnd;
    final picked  = await showTimePicker(context: context, initialTime: initial);
    if (picked == null || !mounted) return;
    setState(() {
      if (isStart) _stressStart = picked;
      else         _stressEnd   = picked;
    });
  }

  // ── Report generation ─────────────────────────────────

  Future<void> _generate() async {
    if (_fromDt.isAfter(_toDt)) {
      setState(() => _errorMsg = '"From" must be before "To".');
      return;
    }
    setState(() { _loading = true; _errorMsg = null; });

    try {
      final uri = Uri.parse(
          "$_baseUrl/api/phones/activity"
          "?from=${Uri.encodeQueryComponent(_fromDt.toIso8601String())}"
          "&to=${Uri.encodeQueryComponent(_toDt.toIso8601String())}");

      final res = await http.get(uri).timeout(const Duration(seconds: 20));
      if (res.statusCode != 200) {
        final body = jsonDecode(res.body);
        setState(() => _errorMsg = body['message'] ?? 'Server error ${res.statusCode}');
        return;
      }
      final body = jsonDecode(res.body);
      final data = body['data'] as Map<String, dynamic>;

      // Build and preview the PDF
      final doc = await _buildPdf(data);
      if (!mounted) return;

      await Printing.layoutPdf(
        onLayout: (_) async => doc.save(),
        name: 'PhoneActivity_${DateFormat("yyyyMMdd_HHmm").format(_fromDt)}.pdf',
      );
    } catch (e) {
      if (mounted) setState(() => _errorMsg = 'Error: $e');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  // ══════════════════════════════════════════════════════
  // PDF BUILDER
  // ══════════════════════════════════════════════════════

  Future<pw.Document> _buildPdf(Map<String, dynamic> data) async {
    final doc   = pw.Document();
    final font  = await PdfGoogleFonts.notoSansRegular();
    final fontB = await PdfGoogleFonts.notoSansBold();

    final sec1 = (data['deposited_and_withdrawn'] as List).cast<Map<String, dynamic>>();
    final sec2 = (data['withdrawn_only']          as List).cast<Map<String, dynamic>>();
    final sec3 = (data['deposited_only']          as List).cast<Map<String, dynamic>>();

    final style     = pw.TextStyle(font: font,  fontSize: 9);
    final styleBold = pw.TextStyle(font: fontB, fontSize: 9);

    // ── Stress check helper ───────────────────────────
    bool isStressed(String? isoTs) {
      if (!_stressEnabled || isoTs == null) return false;
      final dt = DateTime.tryParse(isoTs)?.toLocal();
      if (dt == null) return false;
      final minuteOfDay = dt.hour * 60 + dt.minute;
      final startMin    = _stressStart.hour * 60 + _stressStart.minute;
      final endMin      = _stressEnd.hour   * 60 + _stressEnd.minute;
      // Stressed = outside normal hours
      return minuteOfDay < startMin || minuteOfDay > endMin;
    }

    // ── Determine if a whole section-1 row is stressed ─
    // (either deposit or withdrawal outside hours)
    bool rowStressed1(Map r) =>
        isStressed(r['stored_at'] as String?) ||
        isStressed(r['retrieved_at'] as String?);

    PdfColor rowBg(bool stressed) =>
        stressed ? PdfColor.fromHex('#FFF3CD') : PdfColors.white;

    // ── Group records by student ──────────────────────
    Map<String, List<Map<String, dynamic>>> _byStudent(
        List<Map<String, dynamic>> records) {
      final map = <String, List<Map<String, dynamic>>>{};
      for (final r in records) {
        map.putIfAbsent(r['sid'] as String, () => []).add(r);
      }
      return Map.fromEntries(
          map.entries.toList()..sort((a, b) => a.key.compareTo(b.key)));
    }

    String _studentLabel(List<Map<String, dynamic>> rows) {
      final r = rows.first;
      final last  = (r['last_name']  as String).toUpperCase();
      final first =  r['first_name'] as String;
      return '${r['sid']} — $last $first';
    }

    // ── Cell padding shorthand ────────────────────────
    const cp = pw.EdgeInsets.symmetric(horizontal: 5, vertical: 4);

    // ── Table builders ────────────────────────────────

    // Section 1: PID | Model | Deposited | Withdrawn
    pw.Widget _table1(List<Map<String, dynamic>> rows) {
      return pw.Table(
        border: pw.TableBorder.all(color: PdfColors.grey400, width: 0.5),
        columnWidths: const {
          0: pw.FlexColumnWidth(2.8),
          1: pw.FlexColumnWidth(2),
          2: pw.FlexColumnWidth(2.2),
          3: pw.FlexColumnWidth(2.2),
        },
        children: [
          // Header
          pw.TableRow(
            decoration: const pw.BoxDecoration(color: PdfColor.fromInt(0xFF2C2C2E)),
            children: [
              pw.Padding(padding: cp, child: pw.Text('PID', style: pw.TextStyle(font: fontB, fontSize: 9, color: PdfColors.white))),
              pw.Padding(padding: cp, child: pw.Text('Model', style: pw.TextStyle(font: fontB, fontSize: 9, color: PdfColors.white))),
              pw.Padding(padding: cp, child: pw.Text('Deposited', style: pw.TextStyle(font: fontB, fontSize: 9, color: PdfColors.white))),
              pw.Padding(padding: cp, child: pw.Text('Withdrawn', style: pw.TextStyle(font: fontB, fontSize: 9, color: PdfColors.white))),
            ],
          ),
          ...rows.map((r) {
            final bg      = rowBg(rowStressed1(r));
            final pidShort = (r['pid'] as String).length > 20
                ? '…${(r['pid'] as String).substring((r['pid'] as String).length - 14)}'
                : r['pid'] as String;
            final stressedDep = isStressed(r['stored_at']    as String?);
            final stressedWdw = isStressed(r['retrieved_at'] as String?);
            return pw.TableRow(
              decoration: pw.BoxDecoration(color: bg),
              children: [
                pw.Padding(padding: cp, child: pw.Text(pidShort, style: style)),
                pw.Padding(padding: cp, child: pw.Text(r['model'] as String? ?? '?', style: style)),
                pw.Padding(padding: cp, child: pw.Text(_fmt(r['stored_at'] as String?),
                    style: stressedDep
                        ? pw.TextStyle(font: fontB, fontSize: 9, color: PdfColor.fromHex('#E65100'))
                        : style)),
                pw.Padding(padding: cp, child: pw.Text(_fmt(r['retrieved_at'] as String?),
                    style: stressedWdw
                        ? pw.TextStyle(font: fontB, fontSize: 9, color: PdfColor.fromHex('#E65100'))
                        : style)),
              ],
            );
          }),
        ],
      );
    }

    // Section 2: PID | Model | Withdrawn
    pw.Widget _table2(List<Map<String, dynamic>> rows) {
      return pw.Table(
        border: pw.TableBorder.all(color: PdfColors.grey400, width: 0.5),
        columnWidths: const {
          0: pw.FlexColumnWidth(3),
          1: pw.FlexColumnWidth(2.5),
          2: pw.FlexColumnWidth(2.5),
        },
        children: [
          pw.TableRow(
            decoration: const pw.BoxDecoration(color: PdfColor.fromInt(0xFF2C2C2E)),
            children: [
              pw.Padding(padding: cp, child: pw.Text('PID',       style: pw.TextStyle(font: fontB, fontSize: 9, color: PdfColors.white))),
              pw.Padding(padding: cp, child: pw.Text('Model',     style: pw.TextStyle(font: fontB, fontSize: 9, color: PdfColors.white))),
              pw.Padding(padding: cp, child: pw.Text('Withdrawn', style: pw.TextStyle(font: fontB, fontSize: 9, color: PdfColors.white))),
            ],
          ),
          ...rows.map((r) {
            final bg      = rowBg(isStressed(r['retrieved_at'] as String?));
            final pidShort = (r['pid'] as String).length > 20
                ? '…${(r['pid'] as String).substring((r['pid'] as String).length - 14)}'
                : r['pid'] as String;
            return pw.TableRow(
              decoration: pw.BoxDecoration(color: bg),
              children: [
                pw.Padding(padding: cp, child: pw.Text(pidShort, style: style)),
                pw.Padding(padding: cp, child: pw.Text(r['model'] as String? ?? '?', style: style)),
                pw.Padding(padding: cp, child: pw.Text(_fmt(r['retrieved_at'] as String?),
                    style: isStressed(r['retrieved_at'] as String?)
                        ? pw.TextStyle(font: fontB, fontSize: 9, color: PdfColor.fromHex('#E65100'))
                        : style)),
              ],
            );
          }),
        ],
      );
    }

    // Section 3: PID | Model | Deposited
    pw.Widget _table3(List<Map<String, dynamic>> rows) {
      return pw.Table(
        border: pw.TableBorder.all(color: PdfColors.grey400, width: 0.5),
        columnWidths: const {
          0: pw.FlexColumnWidth(3),
          1: pw.FlexColumnWidth(2.5),
          2: pw.FlexColumnWidth(2.5),
        },
        children: [
          pw.TableRow(
            decoration: const pw.BoxDecoration(color: PdfColor.fromInt(0xFF2C2C2E)),
            children: [
              pw.Padding(padding: cp, child: pw.Text('PID',       style: pw.TextStyle(font: fontB, fontSize: 9, color: PdfColors.white))),
              pw.Padding(padding: cp, child: pw.Text('Model',     style: pw.TextStyle(font: fontB, fontSize: 9, color: PdfColors.white))),
              pw.Padding(padding: cp, child: pw.Text('Deposited', style: pw.TextStyle(font: fontB, fontSize: 9, color: PdfColors.white))),
            ],
          ),
          ...rows.map((r) {
            final bg = rowBg(isStressed(r['stored_at'] as String?));
            final pidShort = (r['pid'] as String).length > 20
                ? '…${(r['pid'] as String).substring((r['pid'] as String).length - 14)}'
                : r['pid'] as String;
            return pw.TableRow(
              decoration: pw.BoxDecoration(color: bg),
              children: [
                pw.Padding(padding: cp, child: pw.Text(pidShort, style: style)),
                pw.Padding(padding: cp, child: pw.Text(r['model'] as String? ?? '?', style: style)),
                pw.Padding(padding: cp, child: pw.Text(_fmt(r['stored_at'] as String?),
                    style: isStressed(r['stored_at'] as String?)
                        ? pw.TextStyle(font: fontB, fontSize: 9, color: PdfColor.fromHex('#E65100'))
                        : style)),
              ],
            );
          }),
        ],
      );
    }

    // ── Section widget ────────────────────────────────
    pw.Widget _buildSection(
      String title,
      String subtitle,
      List<Map<String, dynamic>> records,
      pw.Widget Function(List<Map<String, dynamic>>) tableBuilder,
    ) {
      final grouped = _byStudent(records);

      final children = <pw.Widget>[
        // Section title bar
        pw.Container(
          color: PdfColor.fromHex('#1C1C1E'),
          padding: const pw.EdgeInsets.symmetric(horizontal: 8, vertical: 5),
          child: pw.Row(
            mainAxisAlignment: pw.MainAxisAlignment.spaceBetween,
            children: [
              pw.Text(title,
                  style: pw.TextStyle(
                      font: fontB, fontSize: 11, color: PdfColors.white)),
              pw.Text(subtitle,
                  style: pw.TextStyle(font: font, fontSize: 8.5,
                      color: PdfColors.grey300)),
            ],
          ),
        ),
        pw.SizedBox(height: 6),
      ];

      if (records.isEmpty) {
        children.addAll([
          pw.Padding(
            padding: const pw.EdgeInsets.symmetric(horizontal: 4, vertical: 8),
            child: pw.Text('No entries in this category.',
                style: pw.TextStyle(font: font, fontSize: 9, color: PdfColors.grey600)),
          ),
        ]);
      } else {
        for (final entry in grouped.entries) {
          final sid   = entry.key;
          final rows  = entry.value;
          final label = _studentLabel(rows);

          children.addAll([
            pw.Container(
              padding: const pw.EdgeInsets.symmetric(horizontal: 6, vertical: 3),
              decoration: pw.BoxDecoration(
                color: PdfColor.fromHex('#F0F0F0'),
                border: pw.Border.all(color: PdfColors.grey400, width: 0.5),
              ),
              child: pw.Text(label,
                  style: pw.TextStyle(font: fontB, fontSize: 9)),
            ),
            tableBuilder(rows),
            pw.SizedBox(height: 8),
          ]);
        }
      }

      // Total row
      children.addAll([
        pw.Divider(color: PdfColors.grey400, thickness: 0.5),
        pw.Padding(
          padding: const pw.EdgeInsets.only(top: 2, bottom: 8),
          child: pw.Text('Total entries: ${records.length}',
              style: pw.TextStyle(font: fontB, fontSize: 9)),
        ),
      ]);

      // Wrap in a KeepTogether to avoid orphaned headers
      return pw.Column(
        crossAxisAlignment: pw.CrossAxisAlignment.stretch,
        children: children,
      );
    }

    // ── Page header ───────────────────────────────────
    pw.Widget _pageHeader() => pw.Column(
          crossAxisAlignment: pw.CrossAxisAlignment.stretch,
          children: [
            pw.Row(
                mainAxisAlignment: pw.MainAxisAlignment.spaceBetween,
                children: [
                  pw.Text('PHONE ACTIVITY REPORT',
                      style: pw.TextStyle(font: fontB, fontSize: 14)),
                  pw.Text('Generated: ${_hdrFmt.format(DateTime.now())}',
                      style: pw.TextStyle(font: font, fontSize: 8.5,
                          color: PdfColors.grey600)),
                ]),
            pw.SizedBox(height: 4),
            pw.Text(
                'Period: ${_dtFmt.format(_fromDt)}  →  ${_dtFmt.format(_toDt)}',
                style: pw.TextStyle(font: font, fontSize: 9)),
            if (_stressEnabled) ...[
              pw.SizedBox(height: 2),
              pw.RichText(
                text: pw.TextSpan(children: [
                  pw.TextSpan(
                      text: 'Normal hours: ',
                      style: pw.TextStyle(font: font, fontSize: 9)),
                  pw.TextSpan(
                      text: '${_stressStart.format(context)}  →  ${_stressEnd.format(context)}',
                      style: pw.TextStyle(font: fontB, fontSize: 9)),
                  pw.TextSpan(
                      text: '   ■ Bold orange = outside normal hours',
                      style: pw.TextStyle(
                          font: font, fontSize: 8.5,
                          color: PdfColor.fromHex('#E65100'))),
                ]),
              ),
            ],
            pw.SizedBox(height: 3),
            pw.Divider(thickness: 1),
            pw.SizedBox(height: 8),
          ],
        );

    // ── Assemble pages ────────────────────────────────
    final allContent = <pw.Widget>[
      _pageHeader(),
      _buildSection(
        '1.  DEPOSITED AND WITHDRAWN',
        'Both operations within the interval',
        sec1,
        _table1,
      ),
      pw.SizedBox(height: 12),
      _buildSection(
        '2.  WITHDRAWN WITHOUT DEPOSIT',
        'Withdrawal in interval — deposit outside or prior',
        sec2,
        _table2,
      ),
      pw.SizedBox(height: 12),
      _buildSection(
        '3.  DEPOSITED WITHOUT WITHDRAWAL',
        'Deposit in interval — phone still stored or retrieved later',
        sec3,
        _table3,
      ),
    ];

    doc.addPage(
      pw.MultiPage(
        pageFormat: PdfPageFormat.a4,
        margin: const pw.EdgeInsets.all(28),
        footer: (ctx) => pw.Align(
          alignment: pw.Alignment.centerRight,
          child: pw.Text('Page ${ctx.pageNumber} / ${ctx.pagesCount}',
              style: pw.TextStyle(font: font, fontSize: 8, color: PdfColors.grey500)),
        ),
        build: (_) => allContent,
      ),
    );

    return doc;
  }

  // ══════════════════════════════════════════════════════
  // UI
  // ══════════════════════════════════════════════════════

  @override
  Widget build(BuildContext context) {
    final periodLabel =
        '${_dtFmt.format(_fromDt)}  →  ${_dtFmt.format(_toDt)}';

    return Scaffold(
      backgroundColor: const Color(0xFF0D0D0F),
      appBar: AppBar(
        backgroundColor: const Color(0xFF1C1C1E),
        title: const Text('Activity Report'),
        foregroundColor: Colors.white,
      ),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(20),
          children: [
            // ── Period card ────────────────────────────
            _card(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  _sectionLabel('REPORT PERIOD'),
                  const SizedBox(height: 12),
                  // From row
                  _label('From'),
                  const SizedBox(height: 6),
                  Row(children: [
                    Expanded(child: _dateTile(
                      label: DateFormat('dd/MM/yyyy').format(_fromDate),
                      icon: Icons.calendar_today_outlined,
                      onTap: () => _pickDate(true),
                    )),
                    const SizedBox(width: 10),
                    _timeTile(
                      label: _fromTime.format(context),
                      onTap: () => _pickTime(true),
                    ),
                  ]),
                  const SizedBox(height: 12),
                  // To row
                  _label('To'),
                  const SizedBox(height: 6),
                  Row(children: [
                    Expanded(child: _dateTile(
                      label: DateFormat('dd/MM/yyyy').format(_toDate),
                      icon: Icons.calendar_today_outlined,
                      onTap: () => _pickDate(false),
                    )),
                    const SizedBox(width: 10),
                    _timeTile(
                      label: _toTime.format(context),
                      onTap: () => _pickTime(false),
                    ),
                  ]),
                  const SizedBox(height: 10),
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                    decoration: BoxDecoration(
                      color: Colors.blue.withOpacity(0.1),
                      borderRadius: BorderRadius.circular(8),
                      border: Border.all(color: Colors.blue.withOpacity(0.3)),
                    ),
                    child: Row(children: [
                      const Icon(Icons.access_time, color: Colors.blue, size: 16),
                      const SizedBox(width: 8),
                      Expanded(child: Text(periodLabel,
                          style: const TextStyle(
                              color: Colors.blue, fontSize: 13))),
                    ]),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 16),

            // ── Stress hours card ──────────────────────
            _card(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      _sectionLabel('STRESS HIGHLIGHTING'),
                      Switch(
                        value: _stressEnabled,
                        activeColor: Colors.orange,
                        onChanged: (v) => setState(() => _stressEnabled = v),
                      ),
                    ],
                  ),
                  Text(
                    'Highlight operations outside normal working hours '
                    'in the PDF (bold orange text).',
                    style: const TextStyle(color: Colors.white54, fontSize: 12,
                        height: 1.4),
                  ),
                  if (_stressEnabled) ...[
                    const SizedBox(height: 14),
                    _label('Normal hours'),
                    const SizedBox(height: 8),
                    Row(children: [
                      _timeTile(
                        label: 'From  ${_stressStart.format(context)}',
                        onTap: () => _pickStressTime(true),
                        color: Colors.orange,
                      ),
                      const SizedBox(width: 10),
                      _timeTile(
                        label: 'To  ${_stressEnd.format(context)}',
                        onTap: () => _pickStressTime(false),
                        color: Colors.orange,
                      ),
                    ]),
                    const SizedBox(height: 8),
                    Container(
                      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                      decoration: BoxDecoration(
                        color: Colors.orange.withOpacity(0.1),
                        borderRadius: BorderRadius.circular(8),
                        border: Border.all(color: Colors.orange.withOpacity(0.3)),
                      ),
                      child: Row(children: [
                        const Icon(Icons.warning_amber_outlined,
                            color: Colors.orange, size: 16),
                        const SizedBox(width: 8),
                        Expanded(
                          child: Text(
                            'Operations outside '
                            '${_stressStart.format(context)} – ${_stressEnd.format(context)} '
                            'will be highlighted.',
                            style: const TextStyle(
                                color: Colors.orange, fontSize: 12),
                          ),
                        ),
                      ]),
                    ),
                  ],
                ],
              ),
            ),
            const SizedBox(height: 16),

            // ── Error banner ───────────────────────────
            if (_errorMsg != null) ...[
              Container(
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: Colors.red.withOpacity(0.15),
                  borderRadius: BorderRadius.circular(10),
                  border: Border.all(color: Colors.red.withOpacity(0.4)),
                ),
                child: Row(children: [
                  const Icon(Icons.error_outline,
                      color: Colors.redAccent, size: 18),
                  const SizedBox(width: 10),
                  Expanded(
                      child: Text(_errorMsg!,
                          style: const TextStyle(color: Colors.redAccent))),
                ]),
              ),
              const SizedBox(height: 16),
            ],

            // ── Generate button ────────────────────────
            ElevatedButton.icon(
              icon: _loading
                  ? const SizedBox(
                      width: 18, height: 18,
                      child: CircularProgressIndicator(
                          strokeWidth: 2, color: Colors.white))
                  : const Icon(Icons.picture_as_pdf_outlined),
              label: Text(_loading ? 'Generating…' : 'Generate & Preview PDF'),
              style: ElevatedButton.styleFrom(
                backgroundColor: Colors.deepOrangeAccent,
                foregroundColor: Colors.white,
                minimumSize: const Size(double.infinity, 52),
                shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(12)),
                textStyle: const TextStyle(
                    fontSize: 15, fontWeight: FontWeight.w600),
              ),
              onPressed: _loading ? null : _generate,
            ),
            const SizedBox(height: 8),
            const Text(
              'The PDF will open for preview. From there you can print '
              'or share it.',
              textAlign: TextAlign.center,
              style: TextStyle(color: Colors.white38, fontSize: 12),
            ),
          ],
        ),
      ),
    );
  }

  // ── Small reusable widgets ────────────────────────────

  Widget _card({required Widget child}) => Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color: const Color(0xFF1C1C1E),
          borderRadius: BorderRadius.circular(14),
          border: Border.all(color: Colors.white.withOpacity(0.08)),
        ),
        child: child,
      );

  Widget _sectionLabel(String text) => Text(text,
      style: TextStyle(
          fontSize: 11,
          fontWeight: FontWeight.w700,
          color: Colors.white.withOpacity(0.4),
          letterSpacing: 0.9));

  Widget _label(String text) => Text(text,
      style: const TextStyle(color: Colors.white70, fontSize: 13));

  Widget _dateTile({
    required String label,
    required IconData icon,
    required VoidCallback onTap,
  }) =>
      InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(10),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
          decoration: BoxDecoration(
            color: Colors.white.withOpacity(0.05),
            borderRadius: BorderRadius.circular(10),
            border: Border.all(color: Colors.white.withOpacity(0.12)),
          ),
          child: Row(children: [
            Icon(icon, color: Colors.white54, size: 16),
            const SizedBox(width: 8),
            Text(label, style: const TextStyle(color: Colors.white, fontSize: 14)),
          ]),
        ),
      );

  Widget _timeTile({
    required String label,
    required VoidCallback onTap,
    Color color = Colors.white,
  }) =>
      InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(10),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
          decoration: BoxDecoration(
            color: color.withOpacity(0.07),
            borderRadius: BorderRadius.circular(10),
            border: Border.all(color: color.withOpacity(0.2)),
          ),
          child: Row(mainAxisSize: MainAxisSize.min, children: [
            Icon(Icons.access_time_outlined, color: color.withOpacity(0.7), size: 16),
            const SizedBox(width: 6),
            Text(label,
                style: TextStyle(color: color, fontSize: 14,
                    fontWeight: FontWeight.w500)),
          ]),
        ),
      );
}