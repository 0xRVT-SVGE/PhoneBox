import 'dart:async';
import 'dart:convert';
import 'package:http/http.dart' as http;
import 'package:flutter_webrtc/flutter_webrtc.dart';

class ApiService {
  static const String baseUrl = "http://localhost:5000";

  // ====================== STUDENTS ======================

  static Future<List<dynamic>?> getStudents() async {
    final res = await http.get(Uri.parse("$baseUrl/api/students")).timeout(const Duration(seconds: 8));
    if (res.statusCode == 200) return jsonDecode(res.body)["data"];
    return null;
  }

  static Future<Map<String, dynamic>?> getStudent(String sid) async {
    final res = await http.get(Uri.parse("$baseUrl/api/students/$sid")).timeout(const Duration(seconds: 8));
    if (res.statusCode == 200) return jsonDecode(res.body)["data"];
    return null;
  }

  static Future<bool> createStudent(Map<String, dynamic> data) async {
    try {
      final res = await http.post(
        Uri.parse("$baseUrl/api/students"),
        headers: {"Content-Type": "application/json"},
        body: jsonEncode(data),
      ).timeout(const Duration(seconds: 8));
      return res.statusCode == 200 || res.statusCode == 201;
    } catch (e) {
      print("createStudent error: $e");
      return false;
    }
  }

  static Future<bool> updateStudent(
      String sid, Map<String, dynamic> data) async {
    final res = await http.put(
      Uri.parse("$baseUrl/api/students/$sid"),
      headers: {"Content-Type": "application/json"},
      body: jsonEncode(data),
    ).timeout(const Duration(seconds: 8));
    return res.statusCode == 200;
  }

  static Future<bool> deleteStudent(String sid) async {
    final res = await http.delete(Uri.parse("$baseUrl/api/students/$sid")).timeout(const Duration(seconds: 8));
    return res.statusCode == 200;
  }

  static Future<List<Map<String, dynamic>>> searchStudents(
      String query) async {
    try {
      final uri = Uri.parse("$baseUrl/api/students/search?q=$query");
      final response = await http.get(uri).timeout(const Duration(seconds: 8));
      if (response.statusCode == 200) {
        final jsonData = jsonDecode(response.body);
        if (jsonData['status'] == 'success') {
          final List<dynamic> data = jsonData['data'];
          return data.cast<Map<String, dynamic>>();
        }
      }
      return [];
    } catch (e) {
      print("Search error: $e");
      return [];
    }
  }

  // ====================== PHONES ======================

  static Future<List<dynamic>?> getPhones(String sid) async {
    final res = await http.get(Uri.parse("$baseUrl/api/phones/$sid")).timeout(const Duration(seconds: 8));
    if (res.statusCode == 200) return jsonDecode(res.body)["data"];
    return null;
  }

  static Future<bool> createPhone(Map<String, dynamic> data) async {
    final res = await http.post(
      Uri.parse("$baseUrl/api/phones/"),
      headers: {"Content-Type": "application/json"},
      body: jsonEncode(data),
    ).timeout(const Duration(seconds: 8));
    return res.statusCode == 200 || res.statusCode == 201;
  }

  static Future<bool> updatePhone(
      String pid, Map<String, dynamic> data) async {
    final res = await http.put(
      Uri.parse("$baseUrl/api/phones/$pid"),
      headers: {"Content-Type": "application/json"},
      body: jsonEncode(data),
    ).timeout(const Duration(seconds: 8));
    return res.statusCode == 200;
  }

  static Future<bool> deletePhone(String pid) async {
    final res = await http.delete(Uri.parse("$baseUrl/api/phones/$pid")).timeout(const Duration(seconds: 8));
    return res.statusCode == 200;
  }

  static Future<bool> takePhone(String pid) async {
    final res = await http.post(
      Uri.parse("$baseUrl/api/phones/take"),
      headers: {"Content-Type": "application/json"},
      body: jsonEncode({"pid": pid}),
    ).timeout(const Duration(seconds: 8));
    return res.statusCode == 200;
  }

  static Future<bool> putPhone(String pid) async {
    final res = await http.post(
      Uri.parse("$baseUrl/api/phones/put"),
      headers: {"Content-Type": "application/json"},
      body: jsonEncode({"pid": pid}),
    ).timeout(const Duration(seconds: 8));
    return res.statusCode == 200;
  }

  // ====================== ADMIN LISTING ======================

  static Future<List<dynamic>?> getAllPhones() async {
    final res = await http.get(Uri.parse("$baseUrl/api/phones")).timeout(const Duration(seconds: 8));
    if (res.statusCode == 200) return jsonDecode(res.body)["data"];
    return null;
  }

  static Future<List<dynamic>?> getStoredPhones() async {
    final res = await http.get(Uri.parse("$baseUrl/api/phones/stored")).timeout(const Duration(seconds: 8));
    if (res.statusCode == 200) return jsonDecode(res.body)["data"];
    return null;
  }

  static Future<List<dynamic>?> getTakenPhones() async {
    final res = await http.get(Uri.parse("$baseUrl/api/phones/taken")).timeout(const Duration(seconds: 8));
    if (res.statusCode == 200) return jsonDecode(res.body)["data"];
    return null;
  }

  // ====================== WEBRTC ======================

  /// Send a WebRTC offer to the server, retrying on network failure.
  ///
  /// [mode] must be one of: 'main' | 'preview' | 'admin'
  /// [maxRetries] how many attempts before giving up (default 5).
  /// [retryDelay] base delay between attempts — doubles each retry.
  ///
  /// Returns the answer SDP string, or null if all attempts fail.
  static Future<String?> sendOffer(
      String offerSDP, {
        String mode = 'main',
        int maxRetries = 5,
        Duration retryDelay = const Duration(seconds: 2),
      }) async {
    assert(
    ['main', 'preview', 'admin'].contains(mode),
    'sendOffer: invalid mode "$mode"',
    );

    Duration delay = retryDelay;
    for (int attempt = 1; attempt <= maxRetries; attempt++) {
      try {
        final res = await http
            .post(
          Uri.parse("$baseUrl/webrtc/offer/$mode"),
          headers: {"Content-Type": "application/json"},
          body: jsonEncode({"sdp": offerSDP, "type": "offer"}),
        )
            .timeout(const Duration(seconds: 8));

        if (res.statusCode == 200) {
          final data = jsonDecode(res.body);
          return data["data"]["sdp"] as String?;
        }
        // Non-200 — server is up but rejected; no point retrying
        print("sendOffer($mode): server returned ${res.statusCode}");
        return null;
      } catch (e) {
        // Network error or timeout — retry
        if (attempt == maxRetries) {
          print("sendOffer($mode): all $maxRetries attempts failed. Last error: $e");
          return null;
        }
        print("sendOffer($mode): attempt $attempt failed ($e), retrying in ${delay.inSeconds}s");
        await Future.delayed(delay);
        delay *= 2; // exponential back-off
      }
    }
    return null;
  }

  /// Convenience wrapper for the admin top-down camera stream.
  static Future<String?> sendAdminOffer(String offerSDP) =>
      sendOffer(offerSDP, mode: 'admin');

  static Future<String?> createPreviewOffer(String offerSDP) =>
      sendOffer(offerSDP, mode: 'preview');

  // Legacy method kept for compatibility — unused internally.
  static Future<void> startPreviewConnection(
      Map<String, dynamic> offer,
      RTCVideoRenderer renderer,
      ) async {
    final pc = await createPeerConnection({
      'iceServers': [
        {'urls': 'stun:stun.l.google.com:19302'}
      ]
    });

    pc.onTrack = (event) {
      if (event.track.kind == "video") {
        renderer.srcObject = event.streams[0];
      }
    };

    pc.onIceCandidate = (RTCIceCandidate c) async {
      if (c.candidate != null) {
        await http.post(
          Uri.parse("$baseUrl/webrtc/candidate"),
          headers: {"Content-Type": "application/json"},
          body: jsonEncode({
            'candidate': c.candidate,
            'sdpMid': c.sdpMid,
            'sdpMLineIndex': c.sdpMLineIndex,
            'role': 'preview'
          }),
        );
      }
    };

    await pc.setRemoteDescription(
      RTCSessionDescription(offer["sdp"], offer["type"]),
    );

    final answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);

    await http.post(
      Uri.parse("$baseUrl/webrtc/answer"),
      headers: {"Content-Type": "application/json"},
      body: jsonEncode({
        "sdp": answer.sdp,
        "type": answer.type,
        "role": "preview"
      }),
    );
  }

  static Future<Map<String, dynamic>?> takePhoto() async {
    final res = await http.post(Uri.parse("$baseUrl/webrtc/take_photo")).timeout(const Duration(seconds: 8));
    if (res.statusCode != 200) return null;
    return jsonDecode(res.body);
  }

  /// Cancel an active WebRTC connection by mode.
  static Future<bool> cancelConnection(String mode) async {
    try {
      final res = await http
          .post(Uri.parse("$baseUrl/webrtc/cancel/$mode"))
          .timeout(const Duration(seconds: 4));
      return res.statusCode == 200;
    } catch (e) {
      print("cancelConnection($mode) error: $e");
      return false;
    }
  }

  static Future<bool> cancelPreview() => cancelConnection('preview');
  static Future<bool> cancelMain() => cancelConnection('main');
  static Future<bool> cancelAdmin() => cancelConnection('admin');
}