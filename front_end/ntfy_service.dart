// ============================================================
// FILE: front_end/ntfy_service.dart
// ============================================================
//
// LAN-native push notifications via self-hosted ntfy server.
//
// ntfy is a simple HTTP pub/sub service you can run on your LAN:
//   docker run -d --name ntfy -p 80:80 binwiederhier/ntfy serve
//
// This service:
//   1. Subscribes to the ntfy alarm topic over SSE (Server-Sent Events)
//   2. Shows a local Flutter notification when alarm_triggered arrives
//   3. Routes the user to AlarmPage when they tap the notification
//   4. Works entirely offline / on LAN — no Firebase, no internet
//
// Setup
// ─────
//   1. Add to pubspec.yaml:
//        flutter_local_notifications: ^17.0.0
//        http: ^1.2.2   (already present)
//
//   2. Android: add to AndroidManifest.xml inside <application>:
//        <receiver android:exported="false"
//            android:name="com.dexterous.flutterlocalnotifications.ScheduledNotificationReceiver"/>
//
//   3. Call NtfyService.instance.init(context) from main.dart after runApp()
//
//   4. Set NtfyConfig.SERVER_URL in back_end/config.py to your ntfy host
//      and match NTFY_SERVER / NTFY_TOPIC constants below.
//
// Usage (in scan_page.dart initState):
//   await NtfyService.instance.init(context);

import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:http/http.dart' as http;

// ── Config — must match back_end/config.py NtfyConfig ────────────────────────
const String _kNtfyServer = "http://ntfy.phonebox.local"; // or "http://192.168.1.x"
const String _kNtfyTopic  = "phonebox-alarms";
const String _kNtfyUrl    = "$_kNtfyServer/$_kNtfyTopic/sse";

// ── Local notification channel ────────────────────────────────────────────────
const String _kChannelId   = "phonebox_alarms";
const String _kChannelName = "PhoneBox Alarms";
const int    _kNotifId     = 1001;

// ── Navigation callback — set from main.dart or scan_page.dart ───────────────
// Called when user taps a notification; payload is JSON mismatch list.
typedef OnAlarmTap = void Function(List<dynamic> mismatches);

class NtfyService {
  NtfyService._();
  static final NtfyService instance = NtfyService._();

  final FlutterLocalNotificationsPlugin _localNotif =
      FlutterLocalNotificationsPlugin();

  bool _initialized      = false;
  bool _connected        = false;
  bool _disposed         = false;

  StreamSubscription<String>? _sseSub;
  OnAlarmTap? _onAlarmTap;

  // ── Init ──────────────────────────────────────────────────────────────────

  /// Call once at app startup.
  /// [onAlarmTap] is called when the user taps a push notification;
  /// navigate to AlarmPage from there.
  Future<void> init(
    BuildContext context, {
    OnAlarmTap? onAlarmTap,
  }) async {
    if (_initialized) return;
    _initialized = true;
    _onAlarmTap  = onAlarmTap;

    // Configure local notifications
    const android = AndroidInitializationSettings('@mipmap/ic_launcher');
    const ios     = DarwinInitializationSettings();
    await _localNotif.initialize(
      const InitializationSettings(android: android, iOS: ios),
      onDidReceiveNotificationResponse: _onNotificationTap,
    );

    // Create Android channel
    const channel = AndroidNotificationChannel(
      _kChannelId,
      _kChannelName,
      importance:    Importance.max,
      enableVibration: true,
      playSound:     true,
    );
    await _localNotif
        .resolvePlatformSpecificImplementation<
            AndroidFlutterLocalNotificationsPlugin>()
        ?.createNotificationChannel(channel);

    // Start SSE subscription in background
    _startSubscription();
  }

  void setAlarmTapCallback(OnAlarmTap cb) => _onAlarmTap = cb;

  void dispose() {
    _disposed = true;
    _sseSub?.cancel();
    _sseSub = null;
  }

  bool get isConnected => _connected;

  // ── SSE subscription ──────────────────────────────────────────────────────

  void _startSubscription() {
    // Run in an isolate-friendly loop: reconnect on any error
    _connectLoop();
  }

  Future<void> _connectLoop() async {
    while (!_disposed) {
      try {
        _connected = false;
        await _subscribe();
      } catch (e) {
        _connected = false;
        debugPrint('[NtfyService] SSE error — reconnecting in 5s: $e');
        await Future.delayed(const Duration(seconds: 5));
      }
    }
  }

  Future<void> _subscribe() async {
    final client = http.Client();
    try {
      final request = http.Request("GET", Uri.parse(_kNtfyUrl));
      request.headers['Accept'] = 'text/event-stream';

      final response = await client.send(request).timeout(
        const Duration(seconds: 10),
        onTimeout: () => throw TimeoutException('ntfy connect timeout'),
      );

      if (response.statusCode != 200) {
        throw Exception('ntfy returned ${response.statusCode}');
      }

      _connected = true;
      debugPrint('[NtfyService] Connected to $_kNtfyUrl');

      // Read SSE stream line by line
      final stream = response.stream
          .transform(utf8.decoder)
          .transform(const LineSplitter());

      final buf = StringBuffer();

      await for (final line in stream) {
        if (_disposed) break;

        if (line.startsWith('data:')) {
          buf.write(line.substring(5).trim());
        } else if (line.isEmpty && buf.isNotEmpty) {
          // End of event — process it
          _handleEvent(buf.toString());
          buf.clear();
        }
      }
    } finally {
      client.close();
      _connected = false;
    }
  }

  void _handleEvent(String data) {
    try {
      final Map<String, dynamic> msg = jsonDecode(data);
      final String event  = msg['event'] ?? '';
      final String title  = msg['title'] ?? 'PhoneBox Alarm';
      final String body   = msg['message'] ?? '';

      if (event == 'message') {
        // ntfy sends event:"message" for all published messages.
        // Alarm notifications include mismatches in the attachment field.
        final List<dynamic> mismatches =
            (msg['attachment'] is Map)
                ? (msg['attachment']['mismatches'] ?? [])
                : [];

        debugPrint('[NtfyService] Alarm notification received: $title');
        _showNotification(title, body, mismatches);
      }
    } catch (e) {
      debugPrint('[NtfyService] Failed to parse SSE event: $e');
    }
  }

  // ── Local notification ─────────────────────────────────────────────────────

  Future<void> _showNotification(
    String title,
    String body,
    List<dynamic> mismatches,
  ) async {
    final payload = jsonEncode(mismatches);

    const android = AndroidNotificationDetails(
      _kChannelId,
      _kChannelName,
      importance:    Importance.max,
      priority:      Priority.high,
      fullScreenIntent: true,
      category:      AndroidNotificationCategory.alarm,
    );
    const ios = DarwinNotificationDetails(
      presentAlert: true,
      presentBadge: true,
      presentSound: true,
    );

    await _localNotif.show(
      _kNotifId,
      title,
      body,
      const NotificationDetails(android: android, iOS: ios),
      payload: payload,
    );
  }

  void _onNotificationTap(NotificationResponse response) {
    if (_onAlarmTap == null) return;
    try {
      final List<dynamic> mismatches =
          response.payload != null ? jsonDecode(response.payload!) : [];
      _onAlarmTap!(mismatches);
    } catch (_) {
      _onAlarmTap!([]);
    }
  }
}
