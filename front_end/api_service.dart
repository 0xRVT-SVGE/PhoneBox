import 'dart:convert';
import 'package:http/http.dart' as http;
import 'package:flutter_webrtc/flutter_webrtc.dart';

class ApiService {
  static const String baseUrl = "http://localhost:5000";

  // ====================== STUDENTS ======================

  static Future<List<dynamic>?> getStudents() async {
    final res = await http.get(Uri.parse("$baseUrl/api/students"));
    if (res.statusCode == 200) return jsonDecode(res.body)["data"];
    return null;
  }

  static Future<Map<String, dynamic>?> getStudent(String sid) async {
    final res = await http.get(Uri.parse("$baseUrl/api/students/$sid"));
    if (res.statusCode == 200) return jsonDecode(res.body)["data"];
    return null;
  }

  static Future<bool> createStudent(Map<String, dynamic> data) async {
    try {
      final res = await http.post(
        Uri.parse("$baseUrl/api/students"),
        headers: {"Content-Type": "application/json"},
        body: jsonEncode(data),
      );
      return res.statusCode == 200 || res.statusCode == 201;
    } catch (e) {
      print("createStudent error: $e");
      return false;
    }
  }

  static Future<bool> updateStudent(String sid,
      Map<String, dynamic> data) async {
    final res = await http.put(
      Uri.parse("$baseUrl/api/students/$sid"),
      headers: {"Content-Type": "application/json"},
      body: jsonEncode(data),
    );
    return res.statusCode == 200;
  }

  static Future<bool> deleteStudent(String sid) async {
    final res = await http.delete(Uri.parse("$baseUrl/api/students/$sid"));
    return res.statusCode == 200;
  }

  static Future<List<Map<String, dynamic>>> searchStudents(String query) async {
    try {
      final uri = Uri.parse("$baseUrl/api/students/search?q=$query");
      final response = await http.get(uri);

      if (response.statusCode == 200) {
        final jsonData = jsonDecode(response.body);
        if (jsonData['status'] == 'success') {
          final List<dynamic> data = jsonData['data'];
          return data.cast<Map<String, dynamic>>();
        }
      }
      return []; // empty if no results or error
    } catch (e) {
      print("Search error: $e");
      return [];
    }
  }


  // ====================== PHONES ======================

  static Future<List<dynamic>?> getPhones(String sid) async {
    final res = await http.get(Uri.parse("$baseUrl/api/phones/$sid"));
    if (res.statusCode == 200) return jsonDecode(res.body)["data"];
    return null;
  }

  static Future<Map<String, dynamic>?> getPhone(String pid) async {
    final res = await http.get(Uri.parse("$baseUrl/api/phones/pid/$pid"));
    if (res.statusCode == 200) return jsonDecode(res.body)["data"];
    return null;
  }

  static Future<bool> createPhone(Map<String, dynamic> data) async {
    final res = await http.post(
      Uri.parse("$baseUrl/api/phones/"),
      headers: {"Content-Type": "application/json"},
      body: jsonEncode(data),
    );
    return res.statusCode == 200 || res.statusCode == 201;
  }

  static Future<bool> updatePhone(String pid, Map<String, dynamic> data) async {
    final res = await http.put(
      Uri.parse("$baseUrl/api/phones/$pid"),
      headers: {"Content-Type": "application/json"},
      body: jsonEncode(data),
    );
    return res.statusCode == 200;
  }

  static Future<bool> deletePhone(String pid) async {
    final res = await http.delete(Uri.parse("$baseUrl/api/phones/$pid"));
    return res.statusCode == 200;
  }

  static Future<bool> takePhone(String pid) async {
    final res = await http.post(
      Uri.parse("$baseUrl/api/phones/take"),
      headers: {"Content-Type": "application/json"},
      body: jsonEncode({"pid": pid}),
    );
    return res.statusCode == 200;
  }

  static Future<bool> putPhone(String pid) async {
    final res = await http.post(
      Uri.parse("$baseUrl/api/phones/put"),
      headers: {"Content-Type": "application/json"},
      body: jsonEncode({"pid": pid}),
    );
    return res.statusCode == 200;
  }


  // ====================== ADMIN LISTING ======================

  static Future<List<dynamic>?> getAllPhones() async {
    final res = await http.get(Uri.parse("$baseUrl/api/phones"));
    if (res.statusCode == 200) return jsonDecode(res.body)["data"];
    return null;
  }

  static Future<List<dynamic>?> getStoredPhones() async {
    final res = await http.get(Uri.parse("$baseUrl/api/phones/stored"));
    if (res.statusCode == 200) return jsonDecode(res.body)["data"];
    return null;
  }

  static Future<List<dynamic>?> getTakenPhones() async {
    final res = await http.get(Uri.parse("$baseUrl/api/phones/taken"));
    if (res.statusCode == 200) return jsonDecode(res.body)["data"];
    return null;
  }


  // ====================== WEBRTC ======================

  static Future<String?> sendOffer(String offerSDP) async {
    final res = await http.post(
      Uri.parse("$baseUrl/webrtc/offer/main"),
      headers: {"Content-Type": "application/json"},
      body: jsonEncode({"sdp": offerSDP, "type": "offer"}),
    );

    if (res.statusCode == 200) {
      final data = jsonDecode(res.body);
      return data["data"]["sdp"];
    }
    return null;
  }

  static Future<String?> createPreviewOffer(String offerSDP) async {
    final res = await http.post(
      Uri.parse("$baseUrl/webrtc/offer/preview"),
      headers: {"Content-Type": "application/json"},
      body: jsonEncode({"sdp": offerSDP, "type": "offer"}),
    );

    if (res.statusCode == 200) {
      final data = jsonDecode(res.body);
      return data["data"]?["sdp"];
    }
    return null;
  }

  static Future<void> startPreviewConnection(Map<String, dynamic> offer,
      RTCVideoRenderer renderer) async {
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
    final res = await http.post(Uri.parse("$baseUrl/webrtc/take_photo"));
    if (res.statusCode != 200) return null;
    return jsonDecode(res.body);
  }


  static Future<bool> cancelPreview() async {
    try {
      final res = await http.post(Uri.parse("$baseUrl/webrtc/cancel/preview"));
      return res.statusCode == 200;
    } catch (e) {
      print("cancelPreview error: $e");
      return false;
    }
  }

  static Future<bool> cancelMain() async {
    try {
      final res = await http.post(Uri.parse("$baseUrl/webrtc/cancel/main"));
      return res.statusCode == 200;
    } catch (e) {
      print("cancelMain error: $e");
      return false;
    }
  }
}