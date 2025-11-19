import 'dart:convert';
import 'package:http/http.dart' as http;

class ApiService {
  static const String baseUrl = "http://localhost:5000"; // LAN IP

  static Future<List<dynamic>?> getStudents() async {
    final res = await http.get(Uri.parse("$baseUrl/api/students"));
    if (res.statusCode == 200) return jsonDecode(res.body)["data"];
    return null;
  }

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
}
