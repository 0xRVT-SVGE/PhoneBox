import 'dart:async';
import 'dart:typed_data';
import 'package:dio/dio.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';

// ── Opt #29: singleton Dio instance ──────────────────────────────────────────
// A single Dio instance reuses the underlying HTTP transport across all calls,
// giving persistent keep-alive connections instead of a new TCP handshake for
// every request.  validateStatus: (_) => true means we never throw on status
// codes — callers check res.statusCode explicitly, same as before.
// ─────────────────────────────────────────────────────────────────────────────

class ApiService {
  // A3: driven by --dart-define=API_BASE_URL=http://192.168.x.x:5000 at build
  // time. Falls back to localhost:5000 for the dev emulator / desktop build.
  // Usage: flutter run --dart-define=API_BASE_URL=http://192.168.1.50:5000
  static const String baseUrl = String.fromEnvironment(
    'API_BASE_URL',
    defaultValue: 'http://localhost:5000',
  );

  static final Dio _dio = Dio(
    BaseOptions(
      baseUrl:         baseUrl,
      connectTimeout:  const Duration(seconds: 8),
      receiveTimeout:  const Duration(seconds: 8),
      validateStatus:  (_) => true,   // never throw; we check statusCode manually
      contentType:     Headers.jsonContentType,
      responseType:    ResponseType.json,
    ),
  );

  // ====================== STUDENTS ======================

  static Future<List<dynamic>?> getStudents() async {
    final res = await _dio.get('/api/students');
    if (res.statusCode == 200) return res.data["data"];
    return null;
  }

  static Future<Map<String, dynamic>?> getStudent(String sid) async {
    final res = await _dio.get('/api/students/$sid');
    if (res.statusCode == 200) return res.data["data"];
    return null;
  }

  static Future<bool> createStudent(Map<String, dynamic> data) async {
    try {
      final res = await _dio.post('/api/students', data: data);
      return res.statusCode == 200 || res.statusCode == 201;
    } catch (e) {
      return false;
    }
  }

  static Future<bool> updateStudent(String sid, Map<String, dynamic> data) async {
    final res = await _dio.put('/api/students/$sid', data: data);
    return res.statusCode == 200;
  }

  static Future<bool> deleteStudent(String sid) async {
    final res = await _dio.delete('/api/students/$sid');
    return res.statusCode == 200;
  }

  static Future<List<Map<String, dynamic>>> searchStudents(String query) async {
    try {
      final res = await _dio.get('/api/students/search', queryParameters: {'q': query});
      if (res.statusCode == 200 && res.data['status'] == 'success') {
        final List<dynamic> data = res.data['data'];
        return data.cast<Map<String, dynamic>>();
      }
      return [];
    } catch (e) {
      return [];
    }
  }

  // ====================== PHONES ======================

  static Future<List<dynamic>?> getPhones(String sid) async {
    final res = await _dio.get('/api/phones/$sid');
    if (res.statusCode == 200) return res.data["data"];
    return null;
  }

  static Future<bool> createPhone(Map<String, dynamic> data) async {
    final res = await _dio.post('/api/phones/', data: data);
    return res.statusCode == 200 || res.statusCode == 201;
  }

  static Future<bool> updatePhone(String pid, Map<String, dynamic> data) async {
    final res = await _dio.put('/api/phones/$pid', data: data);
    return res.statusCode == 200;
  }

  static Future<bool> deletePhone(String pid) async {
    final res = await _dio.delete('/api/phones/$pid');
    return res.statusCode == 200;
  }

  static Future<bool> takePhone(String pid) async {
    final res = await _dio.post('/api/phones/take', data: {'pid': pid});
    return res.statusCode == 200;
  }

  static Future<bool> putPhone(String pid) async {
    final res = await _dio.post('/api/phones/put', data: {'pid': pid});
    return res.statusCode == 200;
  }

  // ====================== ADMIN LISTING ======================

  static Future<List<dynamic>?> getAllPhones() async {
    final res = await _dio.get('/api/phones');
    if (res.statusCode == 200) return res.data["data"];
    return null;
  }

  static Future<List<dynamic>?> getStoredPhones() async {
    final res = await _dio.get('/api/phones/stored');
    if (res.statusCode == 200) return res.data["data"];
    return null;
  }

  static Future<List<dynamic>?> getTakenPhones() async {
    final res = await _dio.get('/api/phones/taken');
    if (res.statusCode == 200) return res.data["data"];
    return null;
  }

  // ====================== WEBRTC ======================

  /// Send a WebRTC offer to the server, retrying on network failure.
  ///
  /// [mode] must be one of: 'main' | 'preview' | 'admin'
  /// [maxRetries] how many attempts before giving up (default 5).
  /// [retryDelay] base delay between attempts — doubles each retry.
  ///
  /// F4: base delay is 300ms for LAN (was 2s → 62s total).
  /// 300ms base: 0.3+0.6+1.2+2.4+4.8 = 9.3s total — still enough for a
  /// server restart but 6× faster recovery than the old 2s base.
  ///
  /// Returns the answer SDP string, or null if all attempts fail.
  static Future<String?> sendOffer(
    String offerSDP, {
    String mode = 'main',
    int maxRetries = 5,
    Duration retryDelay = const Duration(milliseconds: 300), // F4: was Duration(seconds: 2)
  }) async {
    assert(
      ['main', 'preview', 'admin'].contains(mode),
      'sendOffer: invalid mode "$mode"',
    );

    Duration delay = retryDelay;
    for (int attempt = 1; attempt <= maxRetries; attempt++) {
      try {
        final res = await _dio.post(
          '/webrtc/offer/$mode',
          data: {'sdp': offerSDP, 'type': 'offer'},
        );

        if (res.statusCode == 200) {
          return res.data["data"]["sdp"] as String?;
        }
        // Non-200 — server rejected; no point retrying
        return null;
      } on DioException catch (e) {
        // Network-level error — retry with exponential back-off
        if (attempt == maxRetries) {
          return null;
        }
        await Future.delayed(delay);
        delay *= 2;
      } catch (_) {
        return null;
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
        await _dio.post(
          '/webrtc/candidate',
          data: {
            'candidate':     c.candidate,
            'sdpMid':        c.sdpMid,
            'sdpMLineIndex': c.sdpMLineIndex,
            'role':          'preview',
          },
        );
      }
    };

    await pc.setRemoteDescription(
      RTCSessionDescription(offer["sdp"], offer["type"]),
    );

    final answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);

    await _dio.post(
      '/webrtc/answer',
      data: {'sdp': answer.sdp, 'type': answer.type, 'role': 'preview'},
    );
  }

  // ====================== EMBED CAPTURE (HTTP, no WebRTC) ======================

  /// GET /api/embed/preview
  ///
  /// Returns a JPEG snapshot of the current front-camera frame as raw bytes,
  /// suitable for Image.memory().  No face detection — fast, call at ~3 fps.
  ///
  /// Returns null if the camera is unavailable or the request fails.
  static Future<Uint8List?> embedPreview() async {
    try {
      final res = await _dio.get<List<int>>(
        '/api/embed/preview',
        options: Options(
          responseType:   ResponseType.bytes,
          receiveTimeout: const Duration(seconds: 3),
          headers: {'Cache-Control': 'no-store'},
        ),
      );
      if (res.statusCode == 200 && res.data != null) {
        return Uint8List.fromList(res.data!);
      }
    } catch (_) {}
    return null;
  }

  /// POST /api/embed/capture
  ///
  /// Grabs the current front-camera frame, runs face detection, and returns:
  ///   embed        : List<double>  — 128-d L2-normalised embedding vector
  ///   preview_jpeg : String?       — base64 JPEG of the captured frame
  ///
  /// Returns null on network failure.  On face-detection failure the server
  /// returns a 422 with {status:"error", message:"No face detected"}; callers
  /// should check the returned map's 'status' field.
  static Future<Map<String, dynamic>?> captureEmbed() async {
    try {
      final res = await _dio.post<Map<String, dynamic>>(
        '/api/embed/capture',
        options: Options(receiveTimeout: const Duration(seconds: 15)),
      );
      if (res.data != null) return res.data;
    } catch (_) {}
    return null;
  }

  static Future<Map<String, dynamic>?> takePhoto() async {
    final res = await _dio.post('/webrtc/take_photo');
    if (res.statusCode != 200) return null;
    return res.data as Map<String, dynamic>?;
  }

  /// Cancel an active WebRTC connection by mode.
  static Future<bool> cancelConnection(String mode) async {
    try {
      final res = await _dio.post(
        '/webrtc/cancel/$mode',
        options: Options(receiveTimeout: const Duration(seconds: 4)),
      );
      return res.statusCode == 200;
    } catch (_) {
      return false;
    }
  }

  static Future<bool> cancelPreview() => cancelConnection('preview');
  static Future<bool> cancelMain()    => cancelConnection('main');
  static Future<bool> cancelAdmin()   => cancelConnection('admin');

  // ====================== REPORTS ======================

  /// F2: fetch activity report using the singleton Dio instance.
  /// Uses the keep-alive connection pool instead of opening a new TCP socket
  /// per report (was raw http.get() in report_page.dart).
  static Future<Map<String, dynamic>?> getActivityReport(
    DateTime from,
    DateTime to,
  ) async {
    try {
      final res = await _dio.get(
        '/api/phones/activity',
        queryParameters: {
          'from': from.toIso8601String(),
          'to':   to.toIso8601String(),
        },
        options: Options(receiveTimeout: const Duration(seconds: 20)),
      );
      if (res.statusCode == 200) return res.data as Map<String, dynamic>?;
      return null;
    } catch (_) {
      return null;
    }
  }
}