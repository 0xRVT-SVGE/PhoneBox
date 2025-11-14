import 'package:flutter/material.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'socket_service.dart';
import 'admin_menu.dart';
import 'auth.dart';
import 'api_service.dart';

class ScanPage extends StatefulWidget {
  const ScanPage({super.key});

  @override
  State<ScanPage> createState() => _ScanPageState();
}

class _ScanPageState extends State<ScanPage> {
  final RTCVideoRenderer _remoteRenderer = RTCVideoRenderer();
  RTCPeerConnection? _peerConnection;

  // --- Status texts ---
  String webrtcStatus = "Connecting...";
  String scanStatus = "Idle";

  // Button state follows backend 'running'
  bool scanning = false;

  final socketService = SocketService();

  @override
  void initState() {
    super.initState();
    _initRenderer();
    _connectSocket();
  }

  Future<void> _initRenderer() async {
    await _remoteRenderer.initialize();
    await _startWebRTC();
  }

  void _connectSocket() {
    socketService.connect((data) {
      if (!mounted) return;

      setState(() {
        // Button state tied to 'running'
        scanning = data["running"] ?? false;

        // Status message independent
        final auth = data["authorized"] ?? false;
        final user = data["user"] ?? "";
        final faceOk = data["face_verified"] ?? false;
        final barcodeOk = data["barcode_verified"] ?? false;
        final currentName = data["current_name"] ?? "Idle";

        if (auth) {
          scanStatus = "Authorized: $user";
        } else if (barcodeOk) {
          scanStatus = "Verifying Face Match: $currentName";
        } else if (scanning) {
          scanStatus = "Scanning...";
        } else {
          scanStatus = "Idle";
        }
      });
    });
  }


  Future<void> _startWebRTC() async {
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
      'offerToReceiveVideo': true,
      'offerToReceiveAudio': false,
    });

    await _peerConnection!.setLocalDescription(offer);

    final answerSDP = await ApiService.sendOffer(offer.sdp!);

    if (answerSDP != null) {
      await _peerConnection!.setRemoteDescription(
        RTCSessionDescription(answerSDP, 'answer'),
      );

      if (mounted) {
        setState(() => webrtcStatus = "WebRTC Connected");
      }
    } else {
      if (mounted) {
        setState(() => webrtcStatus = "WebRTC Error");
      }
    }
  }

  void toggleScan() {
    socketService.toggleScan();
  }

  void _openAdminMenu() async {
    final auth = AuthService();

    if (!auth.isAdmin) {
      final controller = TextEditingController();
      final result = await showDialog(
        context: context,
        builder: (ctx) => AlertDialog(
          title: const Text("Admin Login"),
          content: TextField(
            controller: controller,
            obscureText: true,
            decoration: const InputDecoration(labelText: "Password"),
          ),
          actions: [
            TextButton(
              onPressed: () {
                if (auth.login(controller.text)) {
                  Navigator.pop(ctx, true);
                } else {
                  Navigator.pop(ctx, false);
                }
              },
              child: const Text("Login"),
            )
          ],
        ),
      );

      if (result != true) return;
    }

    if (!mounted) return;
    Navigator.push(context, MaterialPageRoute(builder: (_) => const AdminMenu()));
  }

  @override
  void dispose() {
    _peerConnection?.close();
    _remoteRenderer.dispose();
    socketService.disconnect();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text("Face + Barcode Scanner"),
        actions: [
          IconButton(
            icon: const Icon(Icons.admin_panel_settings),
            onPressed: _openAdminMenu,
          )
        ],
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

            // --- Show statuses ---
            Text(webrtcStatus, style: const TextStyle(fontSize: 16, color: Colors.grey)),
            const SizedBox(height: 4),
            Text(scanStatus, style: const TextStyle(fontSize: 18)),
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
