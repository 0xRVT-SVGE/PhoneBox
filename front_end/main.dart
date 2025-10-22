import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter/material.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:http/http.dart' as http;

void main() {
  runApp(const FaceScanApp());
}

class FaceScanApp extends StatelessWidget {
  const FaceScanApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Face + Barcode Scanner',
      theme: ThemeData.dark(useMaterial3: true),
      home: const ScanPage(),
      debugShowCheckedModeBanner: false,
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
  final String raspberryIp = "localhost";

  final _remoteRenderer = RTCVideoRenderer();
  RTCPeerConnection? _peerConnection;

  @override
  void initState() {
    super.initState();
    initRenderer();
  }

  Future<void> initRenderer() async {
    await _remoteRenderer.initialize();
    await startWebRTC();
  }

  Future<void> startWebRTC() async {
    final config = {
      'iceServers': [
        {'urls': 'stun:stun.l.google.com:19302'},
      ]
    };

    _peerConnection = await createPeerConnection(config);

    _peerConnection!.onTrack = (event) {
      if (event.streams.isNotEmpty) {
        _remoteRenderer.srcObject = event.streams[0];
      }
    };

    final offer = await _peerConnection!.createOffer({
      'offerToReceiveVideo': 1,
      'offerToReceiveAudio': 0,
    });
    await _peerConnection!.setLocalDescription(offer);

    final res = await http.post(
      Uri.parse("http://$raspberryIp:5000/offer"),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({
        'sdp': offer.sdp,
        'type': offer.type,
      }),
    );

    if (res.statusCode == 200) {
      final answer = jsonDecode(res.body);
      await _peerConnection!.setRemoteDescription(
        RTCSessionDescription(answer['sdp'], answer['type']),
      );
      setState(() => status = "✅ WebRTC connected");
    } else {
      setState(() => status = "⚠️ WebRTC failed: ${res.statusCode}");
    }
  }

  Future<void> toggleScan() async {
    final endpoint = scanning ? "stop_scan" : "start_scan";
    final url = Uri.parse("http://$raspberryIp:5000/$endpoint");

    try {
      final res = await http.post(url);
      if (res.statusCode == 200) {
        setState(() {
          scanning = !scanning;
          status = scanning ? "🔍 Scanning..." : "⏹️ Scan stopped";
        });
      } else {
        setState(() => status = "⚠️ Server returned ${res.statusCode}");
        return;
      }
    } catch (_) {
      setState(() => status = "⚠️ Cannot reach server");
      return;
    }

    if (scanning) {
      timer?.cancel();
      timer = Timer.periodic(const Duration(seconds: 1), (_) => checkStatus());
    } else {
      timer?.cancel();
    }
  }

  Future<void> checkStatus() async {
    final url = Uri.parse("http://$raspberryIp:5000/status");
    try {
      final res = await http.get(url);
      if (res.statusCode == 200) {
        final data = jsonDecode(res.body);
        final authorized = data["authorized"] ?? false;
        final user = data["user"] ?? "";

        if (authorized && user.isNotEmpty) {
          setState(() => status = "✅ Access Granted: $user");
          timer?.cancel();
          scanning = false;
        } else if (scanning) {
          setState(() => status = "🔍 Scanning...");
        }
      } else {
        setState(() => status = "⚠️ Status ${res.statusCode}");
      }
    } catch (_) {
      setState(() => status = "⚠️ Cannot fetch status");
    }
  }

  @override
  void dispose() {
    timer?.cancel();
    _peerConnection?.close();
    _remoteRenderer.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text("Face + Barcode Verification"),
        centerTitle: true,
      ),
      body: SafeArea(
        child: Column(
          children: [
            Expanded(
              child: Center(
                child: AspectRatio(
                  aspectRatio: 4 / 3,
                  child: Container(
                    decoration: BoxDecoration(
                      border: Border.all(color: Colors.white24),
                      borderRadius: BorderRadius.circular(12),
                    ),
                    child: RTCVideoView(_remoteRenderer),
                  ),
                ),
              ),
            ),
            const SizedBox(height: 12),
            Text(
              status,
              style: const TextStyle(fontSize: 18),
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 10),
            Padding(
              padding: const EdgeInsets.only(bottom: 16),
              child: ElevatedButton.icon(
                icon: Icon(scanning ? Icons.stop : Icons.play_arrow),
                label: Text(scanning ? "Stop Scan" : "Start Scan"),
                style: ElevatedButton.styleFrom(
                  backgroundColor: scanning ? Colors.red : Colors.green,
                  minimumSize: const Size(160, 45),
                ),
                onPressed: toggleScan,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
