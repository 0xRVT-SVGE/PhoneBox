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

  final String raspberryIp = "localhost"; // change if needed

  // Store video aspect ratio (default 4:3)
  double aspectRatio = 4 / 3;

  @override
  void initState() {
    super.initState();
    if (kIsWeb) {
      ui_web.platformViewRegistry.registerViewFactory(
        'mjpeg-video',
            (int viewId) => html.ImageElement()
          ..src = "http://$raspberryIp:5000/video_feed"
          ..style.border = 'none'
          ..style.width = '100%'
          ..style.height = '100%',
      );
    }

    // Optionally fetch MJPEG frame once to determine native aspect ratio
    fetchAspectRatio();
  }

  Future<void> fetchAspectRatio() async {
    try {
      final res = await http.get(Uri.parse("http://$raspberryIp:5000/video_feed"));
      if (res.statusCode == 200) {
        // Approximation: assume 4:3 if not detectable
        setState(() => aspectRatio = 4 / 3);
      }
    } catch (_) {
      // fallback
      aspectRatio = 4 / 3;
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
      body: LayoutBuilder(
        builder: (context, constraints) {
          // Dynamically calculate feed height based on native aspect ratio
          final maxWidth = constraints.maxWidth;
          final maxHeight = constraints.maxHeight - 150; // leave space for status/buttons
          double feedWidth = maxWidth;
          double feedHeight = feedWidth / aspectRatio;

          if (feedHeight > maxHeight) {
            feedHeight = maxHeight;
            feedWidth = feedHeight * aspectRatio;
          }

          return Center(
            child: Column(
              mainAxisAlignment: MainAxisAlignment.start,
              children: [
                SizedBox(
                  width: feedWidth,
                  height: feedHeight,
                  child: kIsWeb
                      ? const HtmlElementView(viewType: 'mjpeg-video')
                      : Image.network(
                    "http://$raspberryIp:5000/video_feed",
                    fit: BoxFit.contain,
                    gaplessPlayback: true,
                    errorBuilder: (context, error, stackTrace) =>
                    const Center(child: Text("⚠️ Cannot load video feed")),
                  ),
                ),
                const SizedBox(height: 10),
                Flexible(
                  child: Text(
                    status,
                    style: const TextStyle(fontSize: 22),
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
                const SizedBox(height: 10),
                ElevatedButton(
                  onPressed: toggleScan,
                  child: Text(scanning ? "Stop Scan" : "Start Scan"),
                ),
              ],
            ),
          );
        },
      ),
    );
  }
}
