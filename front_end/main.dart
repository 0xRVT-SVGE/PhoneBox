import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import 'dart:ui_web' as ui_web; // for platformViewRegistry
import 'dart:html' as html; // for ImageElement (Web only)

void main() {
  runApp(const FaceScanApp());
}

class FaceScanApp extends StatelessWidget {
  const FaceScanApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Face + Barcode Scanner',
      theme: ThemeData.dark(),
      home: const ScanPage(),
    );
  }
}

class ScanPage extends StatefulWidget {
  const ScanPage({super.key});

  @override
  State<ScanPage> createState() => _ScanPageState();
}

class _ScanPageState extends State<ScanPage> {
  String status = "Idle";
  Timer? timer;
  bool scanning = false;

  final String raspberryIp = "localhost"; // use localhost for web

  @override
  void initState() {
    super.initState();

    if (kIsWeb) {
      // Register HTML <img> view factory for MJPEG feed
      ui_web.platformViewRegistry.registerViewFactory(
        'mjpeg-video',
            (int viewId) => html.ImageElement()
          ..src = "http://$raspberryIp:5000/video_feed"
          ..style.border = 'none'
          ..style.width = '100%'
          ..style.height = '100%',
      );
    }
  }

  Future<void> toggleScan() async {
    final url = Uri.parse(
        "http://$raspberryIp:5000/${scanning ? "stop_scan" : "start_scan"}");

    try {
      await http.post(url);
      setState(() => scanning = !scanning);
    } catch (e) {
      setState(() => status = "⚠️ Cannot reach server");
      return;
    }

    if (scanning) {
      timer?.cancel();
      timer = Timer.periodic(const Duration(seconds: 1), (_) => checkStatus());
    } else {
      timer?.cancel();
      setState(() => status = "Idle");
    }
  }

  Future<void> checkStatus() async {
    final url = Uri.parse("http://$raspberryIp:5000/status");
    try {
      final res = await http.get(url);
      if (res.statusCode == 200) {
        final data = jsonDecode(res.body);
        if (data["authorized"] == true) {
          setState(() => status = "✅ Access Granted: ${data['user']}");
          timer?.cancel();
        } else {
          setState(() => status = "Scanning...");
        }
      }
    } catch (e) {
      setState(() => status = "⚠️ Cannot fetch status");
    }
  }

  @override
  void dispose() {
    timer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text("Face + Barcode Verification")),
      body: Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            SizedBox(
              width: 800,  // bigger MJPEG feed
              height: 600,
              child: kIsWeb
                  ? const HtmlElementView(viewType: 'mjpeg-video')
                  : Image.network(
                "http://$raspberryIp:5000/video_feed",
                gaplessPlayback: true,
                fit: BoxFit.cover,
                errorBuilder: (context, error, stackTrace) =>
                const Text("⚠️ Cannot load video"),
              ),
            ),
            const SizedBox(height: 20),
            Text(status, style: const TextStyle(fontSize: 22)),
            const SizedBox(height: 20),
            ElevatedButton(
              onPressed: toggleScan,
              child: Text(scanning ? "Stop Scan" : "Start Scan"),
            ),
          ],
        ),
      ),
    );
  }
}
