// report_page.dart
import 'dart:typed_data';
import 'package:flutter/foundation.dart'; // compute()
import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:pdf/pdf.dart';
import 'package:pdf/widgets.dart' as pw;
import 'package:printing/printing.dart';
import 'api_service.dart';

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
// F1: top-level adapter required by compute() — must be a
// top-level function, not a closure over instance state.
// Delegates to the static PDF builder on _ReportPageState.
// ══════════════════════════════════════════════════════════

Future<Uint8List> _buildPdfIsolate(Map<String, dynamic> params) =>
    _ReportPageState._buildPdfStatic(params);

// ══════════════════════════════════════════════════════════
// REPORT PAGE
// ══════════════════════════════════════════════════════════

class ReportPage extends StatefulWidget {
  const ReportPage({super.key});

  @override
  State<ReportPage> createState() => _ReportPageState();
}

class _ReportPageState extends State<ReportPage> {
  DateTime _fromDate = DateTime.now().subtract(const Duration(days: 7));
  DateTime _toDate   = DateTime.now();
  TimeOfDay _fromTime = const TimeOfDay(hour: 0, minute: 0);
  TimeOfDay _toTime   = const TimeOfDay(hour: 23, minute: 59);

  bool _stressEnabled = false;
  TimeOfDay _stressStart = const TimeOfDay(hour: 8,  minute: 0);
  TimeOfDay _stressEnd   = const TimeOfDay(hour: 18, minute: 0);

  bool    _loading  = false;
  String? _errorMsg;

  DateTime get _fromDt => DateTime(
      _fromDate.year, _fromDate.month, _fromDate.day,
      _fromTime.hour, _fromTime.minute);

  DateTime get _toDt => DateTime(
      _toDate.year, _toDate.month, _toDate.day,
      _toTime.hour, _toTime.minute);

  Future<void> _pickDate(bool isFrom) async {
    final initial = isFrom ? _fromDate : _toDate;
    final picked  = await showDatePicker(
      context:     context,
      initialDate: initial,
      firstDate:   DateTime(2020),
      lastDate:    DateTime.now().add(const Duration(days: 1)),
    );
    if (picked == null || !mounted) return;
    setState(() { if (isFrom) _fromDate = picked; else _toDate = picked; });
  }

  Future<void> _pickTime(bool isFrom) async {
    final initial = isFrom ? _fromTime : _toTime;
    final picked  = await showTimePicker(context: context, initialTime: initial);
    if (picked == null || !mounted) return;
    setState(() { if (isFrom) _fromTime = picked; else _toTime = picked; });
  }

  Future<void> _pickStressTime(bool isStart) async {
    final initial = isStart ? _stressStart : _stressEnd;
    final picked  = await showTimePicker(context: context, initialTime: initial);
    if (picked == null || !mounted) return;
    setState(() { if (isStart) _stressStart = picked; else _stressEnd = picked; });
  }

  Future<void> _generate() async {
    if (_fromDt.isAfter(_toDt)) {
      setState(() => _errorMsg = '"From" must be before "To".');
      return;
    }
    setState(() { _loading = true; _errorMsg = null; });

    try {
      // F2: use ApiService singleton Dio — reuses keep-alive connection pool
      // instead of opening a new TCP socket for every report.
      final response = await ApiService.getActivityReport(_fromDt, _toDt);
      if (response == null) {
        setState(() => _errorMsg = 'Request failed or timed out.');
        return;
      }
      if (response['status'] != 'success') {
        setState(() => _errorMsg = response['message'] ?? 'Server error');
        return;
      }

      final data = response['data'] as Map<String, dynamic>;

      // F1: build PDF in a background isolate — eliminates UI jank on large
      // reports (500+ records can take 200–600ms on a mid-range tablet).
      // All state the builder needs is serialized into a plain Map so it can
      // cross the isolate boundary via SendPort.
      final params = <String, dynamic>{
        'data':              data,
        'stressEnabled':    _stressEnabled,
        'stressStartHour':  _stressStart.hour,
        'stressStartMin':   _stressStart.minute,
        'stressEndHour':    _stressEnd.hour,
        'stressEndMin':     _stressEnd.minute,
        'fromDtIso':        _fromDt.toIso8601String(),
        'toDtIso':          _toDt.toIso8601String(),
        // Pre-format time labels here — BuildContext can't cross isolate boundary
        'stressStartLabel': _stressStart.format(context),
        'stressEndLabel':   _stressEnd.format(context),
      };

      final bytes = await compute(_buildPdfIsolate, params);
      if (!mounted) return;

      await Printing.layoutPdf(
        onLayout: (_) async => bytes,
        name: 'PhoneActivity_${DateFormat("yyyyMMdd_HHmm").format(_fromDt)}.pdf',
      );
    } catch (e) {
      if (mounted) setState(() => _errorMsg = 'Error: $e');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  // ══════════════════════════════════════════════════════════
  // PDF BUILDER
  // ══════════════════════════════════════════════════════════
  //
  // Fonts: built-in PDF Type1 fonts (Helvetica / Times / Courier).
  // These are part of every PDF reader and require NO network access,
  // NO asset files, and NO internet connection — fully intranet-safe.
  //
  // Note: Type1 fonts cover Latin-1 (ISO-8859-1). For student names
  // that contain only Latin characters this is fine. If your data
  // contains non-Latin scripts (Arabic, CJK, etc.) bundle the
  // NotoSans TTF as a Flutter asset and load with pw.Font.ttf().
  // ══════════════════════════════════════════════════════════

  // F1: static — callable from top-level _buildPdfIsolate via compute().
  // All state arrives in [params]; no instance variables are read.
  static Future<Uint8List> _buildPdfStatic(Map<String, dynamic> params) async {
    final data           = params['data']            as Map<String, dynamic>;
    final stressEnabled  = params['stressEnabled']   as bool;
    final stressStartH   = params['stressStartHour'] as int;
    final stressStartM   = params['stressStartMin']  as int;
    final stressEndH     = params['stressEndHour']   as int;
    final stressEndM     = params['stressEndMin']    as int;
    final fromDt         = DateTime.parse(params['fromDtIso']       as String);
    final toDt           = DateTime.parse(params['toDtIso']         as String);
    final stressStartLbl = params['stressStartLabel'] as String;
    final stressEndLbl   = params['stressEndLabel']   as String;

    final doc = pw.Document();

    // ── Fonts (synchronous, no network, no assets) ────────────────────────
    final font  = pw.Font.helvetica();
    final fontB = pw.Font.helveticaBold();
    final fontM = pw.Font.courier();        // monospace for PID/UUID columns

    final sec1 = (data['deposited_and_withdrawn'] as List).cast<Map<String, dynamic>>();
    final sec2 = (data['withdrawn_only']          as List).cast<Map<String, dynamic>>();
    final sec3 = (data['deposited_only']          as List).cast<Map<String, dynamic>>();

    final style     = pw.TextStyle(font: font,  fontSize: 8);
    final styleMono = pw.TextStyle(font: fontM, fontSize: 7.5);
    final styleBold = pw.TextStyle(font: fontB, fontSize: 8);

    const cp = pw.EdgeInsets.symmetric(horizontal: 4, vertical: 3);

    // ── Stress helpers ────────────────────────────────────────────────────
    bool isStressed(String? isoTs) {
      if (!stressEnabled || isoTs == null) return false;
      final dt = DateTime.tryParse(isoTs)?.toLocal();
      if (dt == null) return false;
      final m = dt.hour * 60 + dt.minute;
      final s = stressStartH * 60 + stressStartM;
      final e = stressEndH   * 60 + stressEndM;
      return m < s || m > e;
    }

    bool rowStressed1(Map r) =>
        isStressed(r['stored_at'] as String?) ||
        isStressed(r['retrieved_at'] as String?);

    PdfColor rowBg(bool stressed, int group) {
      if (stressed) return PdfColor.fromHex('#FFF3CD');
      return group.isOdd ? PdfColors.white : PdfColor.fromInt(0xFFF5F5F5);
    }

    List<Map<String, dynamic>> sortedBySid(List<Map<String, dynamic>> src) {
      final s = List<Map<String, dynamic>>.from(src);
      s.sort((a, b) => (a['sid'] as String).compareTo(b['sid'] as String));
      return s;
    }

    pw.TableRow headerRow(List<String> cols) => pw.TableRow(
      decoration: const pw.BoxDecoration(color: PdfColor.fromInt(0xFF2C2C2E)),
      children: cols.map((c) => pw.Padding(
        padding: cp,
        child: pw.Text(c,
            style: pw.TextStyle(font: fontB, fontSize: 8, color: PdfColors.white)),
      )).toList(),
    );

    String studentCell(Map r, bool isFirst) {
      if (!isFirst) return '';
      final last  = (r['last_name']  as String? ?? '').toUpperCase();
      final first =  r['first_name'] as String? ?? '';
      return '${r['sid']}  $last $first'.trimRight();
    }

    pw.Widget tsCell(String? iso) {
      final stressed = isStressed(iso);
      return pw.Padding(
        padding: cp,
        child: pw.Text(_fmt(iso),
            style: stressed
                ? pw.TextStyle(font: fontB, fontSize: 8,
                    color: PdfColor.fromHex('#E65100'))
                : style),
      );
    }

    // ── TABLE 1 — Deposited AND Withdrawn ─────────────────────────────────
    pw.Widget table1(List<Map<String, dynamic>> records) {
      final sorted = sortedBySid(records);
      final rows   = <pw.TableRow>[
        headerRow(['Student', 'PID', 'Model', 'Deposited', 'Withdrawn']),
      ];

      String? prevSid;
      int grp = 0;
      for (int i = 0; i < sorted.length; i++) {
        final r   = sorted[i];
        final sid = r['sid'] as String;
        if (sid != prevSid) { grp++; prevSid = sid; }

        final isFirst = i == 0 || sorted[i - 1]['sid'] != sid;
        final bg      = rowBg(rowStressed1(r), grp);
        final pid     = r['pid'] as String;

        rows.add(pw.TableRow(
          decoration: pw.BoxDecoration(color: bg),
          children: [
            pw.Padding(padding: cp,
                child: pw.Text(studentCell(r, isFirst), style: style)),
            pw.Padding(padding: cp,
                child: pw.Text(pid, style: styleMono)),
            pw.Padding(padding: cp,
                child: pw.Text(r['model'] as String? ?? '?', style: style)),
            tsCell(r['stored_at']    as String?),
            tsCell(r['retrieved_at'] as String?),
          ],
        ));
      }

      return pw.Table(
        border: pw.TableBorder.all(color: PdfColors.grey400, width: 0.5),
        columnWidths: const {
          0: pw.FlexColumnWidth(2.1),
          1: pw.FlexColumnWidth(3.6),
          2: pw.FlexColumnWidth(1.9),
          3: pw.FlexColumnWidth(2.0),
          4: pw.FlexColumnWidth(2.0),
        },
        children: rows,
      );
    }

    // ── TABLE 2 — Withdrawn only ──────────────────────────────────────────
    pw.Widget table2(List<Map<String, dynamic>> records) {
      final sorted = sortedBySid(records);
      final rows   = <pw.TableRow>[
        headerRow(['Student', 'PID', 'Model', 'Withdrawn']),
      ];

      String? prevSid;
      int grp = 0;
      for (int i = 0; i < sorted.length; i++) {
        final r   = sorted[i];
        final sid = r['sid'] as String;
        if (sid != prevSid) { grp++; prevSid = sid; }

        final isFirst = i == 0 || sorted[i - 1]['sid'] != sid;
        final bg      = rowBg(isStressed(r['retrieved_at'] as String?), grp);
        final pid     = r['pid'] as String;

        rows.add(pw.TableRow(
          decoration: pw.BoxDecoration(color: bg),
          children: [
            pw.Padding(padding: cp,
                child: pw.Text(studentCell(r, isFirst), style: style)),
            pw.Padding(padding: cp,
                child: pw.Text(pid, style: styleMono)),
            pw.Padding(padding: cp,
                child: pw.Text(r['model'] as String? ?? '?', style: style)),
            tsCell(r['retrieved_at'] as String?),
          ],
        ));
      }

      return pw.Table(
        border: pw.TableBorder.all(color: PdfColors.grey400, width: 0.5),
        columnWidths: const {
          0: pw.FlexColumnWidth(2.2),
          1: pw.FlexColumnWidth(3.6),
          2: pw.FlexColumnWidth(2.2),
          3: pw.FlexColumnWidth(2.5),
        },
        children: rows,
      );
    }

    // ── TABLE 3 — Deposited only ──────────────────────────────────────────
    pw.Widget table3(List<Map<String, dynamic>> records) {
      final sorted = sortedBySid(records);
      final rows   = <pw.TableRow>[
        headerRow(['Student', 'PID', 'Model', 'Deposited']),
      ];

      String? prevSid;
      int grp = 0;
      for (int i = 0; i < sorted.length; i++) {
        final r   = sorted[i];
        final sid = r['sid'] as String;
        if (sid != prevSid) { grp++; prevSid = sid; }

        final isFirst = i == 0 || sorted[i - 1]['sid'] != sid;
        final bg      = rowBg(isStressed(r['stored_at'] as String?), grp);
        final pid     = r['pid'] as String;

        rows.add(pw.TableRow(
          decoration: pw.BoxDecoration(color: bg),
          children: [
            pw.Padding(padding: cp,
                child: pw.Text(studentCell(r, isFirst), style: style)),
            pw.Padding(padding: cp,
                child: pw.Text(pid, style: styleMono)),
            pw.Padding(padding: cp,
                child: pw.Text(r['model'] as String? ?? '?', style: style)),
            tsCell(r['stored_at'] as String?),
          ],
        ));
      }

      return pw.Table(
        border: pw.TableBorder.all(color: PdfColors.grey400, width: 0.5),
        columnWidths: const {
          0: pw.FlexColumnWidth(2.2),
          1: pw.FlexColumnWidth(3.6),
          2: pw.FlexColumnWidth(2.2),
          3: pw.FlexColumnWidth(2.5),
        },
        children: rows,
      );
    }

    // ── Section wrapper ───────────────────────────────────────────────────
    pw.Widget buildSection(
      String title,
      String subtitle,
      List<Map<String, dynamic>> records,
      pw.Widget Function(List<Map<String, dynamic>>) tableBuilder,
    ) {
      return pw.Column(
        crossAxisAlignment: pw.CrossAxisAlignment.stretch,
        children: [
          pw.Container(
            color: PdfColor.fromHex('#1C1C1E'),
            padding:
                const pw.EdgeInsets.symmetric(horizontal: 8, vertical: 5),
            child: pw.Row(
              mainAxisAlignment: pw.MainAxisAlignment.spaceBetween,
              children: [
                pw.Text(title,
                    style: pw.TextStyle(
                        font: fontB,
                        fontSize: 11,
                        color: PdfColors.white)),
                pw.Text(subtitle,
                    style: pw.TextStyle(
                        font: font,
                        fontSize: 8.5,
                        color: PdfColors.grey300)),
              ],
            ),
          ),
          pw.SizedBox(height: 6),
          records.isEmpty
              ? pw.Padding(
                  padding: const pw.EdgeInsets.symmetric(
                      horizontal: 4, vertical: 8),
                  child: pw.Text('No entries in this category.',
                      style: pw.TextStyle(
                          font: font,
                          fontSize: 8,
                          color: PdfColors.grey600)),
                )
              : tableBuilder(records),
          pw.SizedBox(height: 4),
          pw.Divider(color: PdfColors.grey400, thickness: 0.5),
          pw.Padding(
            padding: const pw.EdgeInsets.only(top: 2, bottom: 8),
            child: pw.Text('Total entries: ${records.length}',
                style: styleBold),
          ),
        ],
      );
    }

    // ── Page header ───────────────────────────────────────────────────────
    pw.Widget pageHeader() => pw.Column(
          crossAxisAlignment: pw.CrossAxisAlignment.stretch,
          children: [
            pw.Row(
              mainAxisAlignment: pw.MainAxisAlignment.spaceBetween,
              children: [
                pw.Text('PHONE ACTIVITY REPORT',
                    style:
                        pw.TextStyle(font: fontB, fontSize: 14)),
                pw.Text(
                    'Generated: ${_hdrFmt.format(DateTime.now())}',
                    style: pw.TextStyle(
                        font: font,
                        fontSize: 8.5,
                        color: PdfColors.grey600)),
              ],
            ),
            pw.SizedBox(height: 4),
            pw.Text(
                'Period: ${_dtFmt.format(fromDt)}  to  ${_dtFmt.format(toDt)}',
                style: pw.TextStyle(font: font, fontSize: 9)),
            if (stressEnabled) ...[
              pw.SizedBox(height: 2),
              pw.RichText(
                text: pw.TextSpan(children: [
                  pw.TextSpan(
                      text: 'Normal hours: ',
                      style: pw.TextStyle(font: font, fontSize: 9)),
                  pw.TextSpan(
                      text: '$stressStartLbl  ->  $stressEndLbl',
                      style: pw.TextStyle(font: fontB, fontSize: 9)),
                  pw.TextSpan(
                      text:
                          '   Bold = outside normal hours',
                      style: pw.TextStyle(
                          font: font,
                          fontSize: 8.5,
                          color: PdfColor.fromHex('#E65100'))),
                ]),
              ),
            ],
            pw.SizedBox(height: 3),
            pw.Divider(thickness: 1),
            pw.SizedBox(height: 8),
          ],
        );

    // ── Assemble document ─────────────────────────────────────────────────
    doc.addPage(
      pw.MultiPage(
        pageFormat: PdfPageFormat.a4,
        margin:     const pw.EdgeInsets.all(28),
        footer: (ctx) => pw.Align(
          alignment: pw.Alignment.centerRight,
          child: pw.Text(
              'Page ${ctx.pageNumber} / ${ctx.pagesCount}',
              style: pw.TextStyle(
                  font: font, fontSize: 8, color: PdfColors.grey500)),
        ),
        build: (_) => [
          pageHeader(),
          buildSection(
            '1.  DEPOSITED AND WITHDRAWN',
            'Both operations within the interval',
            sec1, table1,
          ),
          pw.SizedBox(height: 12),
          buildSection(
            '2.  WITHDRAWN WITHOUT DEPOSIT',
            'Withdrawal in interval - deposit outside or prior',
            sec2, table2,
          ),
          pw.SizedBox(height: 12),
          buildSection(
            '3.  DEPOSITED WITHOUT WITHDRAWAL',
            'Deposit in interval - phone still stored or retrieved later',
            sec3, table3,
          ),
        ],
      ),
    );

    return doc.save();
  }

  // ══════════════════════════════════════════════════════════
  // UI — unchanged from original
  // ══════════════════════════════════════════════════════════

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
            _card(child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                _sectionLabel('REPORT PERIOD'),
                const SizedBox(height: 12),
                _label('From'),
                const SizedBox(height: 6),
                Row(children: [
                  Expanded(child: _dateTile(
                    label: DateFormat('dd/MM/yyyy').format(_fromDate),
                    icon:  Icons.calendar_today_outlined,
                    onTap: () => _pickDate(true),
                  )),
                  const SizedBox(width: 10),
                  _timeTile(
                      label: _fromTime.format(context),
                      onTap: () => _pickTime(true)),
                ]),
                const SizedBox(height: 12),
                _label('To'),
                const SizedBox(height: 6),
                Row(children: [
                  Expanded(child: _dateTile(
                    label: DateFormat('dd/MM/yyyy').format(_toDate),
                    icon:  Icons.calendar_today_outlined,
                    onTap: () => _pickDate(false),
                  )),
                  const SizedBox(width: 10),
                  _timeTile(
                      label: _toTime.format(context),
                      onTap: () => _pickTime(false)),
                ]),
                const SizedBox(height: 10),
                Container(
                  padding: const EdgeInsets.symmetric(
                      horizontal: 12, vertical: 8),
                  decoration: BoxDecoration(
                    color: Colors.blue.withOpacity(0.1),
                    borderRadius: BorderRadius.circular(8),
                    border: Border.all(
                        color: Colors.blue.withOpacity(0.3)),
                  ),
                  child: Row(children: [
                    const Icon(Icons.access_time,
                        color: Colors.blue, size: 16),
                    const SizedBox(width: 8),
                    Expanded(child: Text(periodLabel,
                        style: const TextStyle(
                            color: Colors.blue, fontSize: 13))),
                  ]),
                ),
              ],
            )),
            const SizedBox(height: 16),

            _card(child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    _sectionLabel('STRESS HIGHLIGHTING'),
                    Switch(
                      value:       _stressEnabled,
                      activeColor: Colors.orange,
                      onChanged:   (v) =>
                          setState(() => _stressEnabled = v),
                    ),
                  ],
                ),
                const Text(
                  'Highlight operations outside normal working hours '
                  'in the PDF (bold orange text).',
                  style: TextStyle(
                      color: Colors.white54, fontSize: 12, height: 1.4),
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
                    padding: const EdgeInsets.symmetric(
                        horizontal: 12, vertical: 8),
                    decoration: BoxDecoration(
                      color: Colors.orange.withOpacity(0.1),
                      borderRadius: BorderRadius.circular(8),
                      border: Border.all(
                          color: Colors.orange.withOpacity(0.3)),
                    ),
                    child: Row(children: [
                      const Icon(Icons.warning_amber_outlined,
                          color: Colors.orange, size: 16),
                      const SizedBox(width: 8),
                      Expanded(child: Text(
                        'Operations outside '
                        '${_stressStart.format(context)} – '
                        '${_stressEnd.format(context)} '
                        'will be highlighted.',
                        style: const TextStyle(
                            color: Colors.orange, fontSize: 12),
                      )),
                    ]),
                  ),
                ],
              ],
            )),
            const SizedBox(height: 16),

            if (_errorMsg != null) ...[
              Container(
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: Colors.red.withOpacity(0.15),
                  borderRadius: BorderRadius.circular(10),
                  border: Border.all(
                      color: Colors.red.withOpacity(0.4)),
                ),
                child: Row(children: [
                  const Icon(Icons.error_outline,
                      color: Colors.redAccent, size: 18),
                  const SizedBox(width: 10),
                  Expanded(child: Text(_errorMsg!,
                      style:
                          const TextStyle(color: Colors.redAccent))),
                ]),
              ),
              const SizedBox(height: 16),
            ],

            ElevatedButton.icon(
              icon: _loading
                  ? const SizedBox(
                      width: 18, height: 18,
                      child: CircularProgressIndicator(
                          strokeWidth: 2, color: Colors.white))
                  : const Icon(Icons.picture_as_pdf_outlined),
              label: Text(_loading
                  ? 'Generating…'
                  : 'Generate & Preview PDF'),
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
              'The PDF will open for preview. From there you can print or share it.',
              textAlign: TextAlign.center,
              style: TextStyle(color: Colors.white38, fontSize: 12),
            ),
          ],
        ),
      ),
    );
  }

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
          padding:
              const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
          decoration: BoxDecoration(
            color: Colors.white.withOpacity(0.05),
            borderRadius: BorderRadius.circular(10),
            border:
                Border.all(color: Colors.white.withOpacity(0.12)),
          ),
          child: Row(children: [
            Icon(icon, color: Colors.white54, size: 16),
            const SizedBox(width: 8),
            Text(label,
                style: const TextStyle(
                    color: Colors.white, fontSize: 14)),
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
          padding:
              const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
          decoration: BoxDecoration(
            color: color.withOpacity(0.07),
            borderRadius: BorderRadius.circular(10),
            border:
                Border.all(color: color.withOpacity(0.2)),
          ),
          child:
              Row(mainAxisSize: MainAxisSize.min, children: [
            Icon(Icons.access_time_outlined,
                color: color.withOpacity(0.7), size: 16),
            const SizedBox(width: 6),
            Text(label,
                style: TextStyle(
                    color: color,
                    fontSize: 14,
                    fontWeight: FontWeight.w500)),
          ]),
        ),
      );
}