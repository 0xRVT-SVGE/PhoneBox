// ============================================================
// FILE: front_end/fcm_service.dart
// ============================================================
//
// Opt #34 — FCM Push Alarms.
//
// Responsibilities
// ────────────────
//   1. Initialize Firebase + firebase_messaging
//   2. Request notification permission (iOS; Android 13+)
//   3. Register the device FCM token with the PhoneBox server
//   4. Handle messages in all three app states:
//      • Foreground  — show an in-app SnackBar / dialog
//      • Background  — notification shown by OS; onMessageOpenedApp
//                      routes the tap to AlarmPage
//      • Terminated  — getInitialMessage() on next app launch
//
// Setup checklist (one-time)
// ──────────────────────────
//   Android:
//     android/app/google-services.json    ← from Firebase console
//     android/app/build.gradle:
//       apply plugin: 'com.google.gms.google-services'
//     android/build.gradle dependencies:
//       classpath 'com.google.gms:google-services:4.4.0'
//     Create notification channel in android/app/src/main/res/values/
//       (or let this service create it programmatically — handled here)
//
//   iOS:
//     ios/Runner/GoogleService-Info.plist ← from Firebase console
//     Enable Push Notifications capability in Xcode
//     Enable Background Modes → Remote notifications in Xcode
//
//   pubspec.yaml:
//     firebase_core: ^3.0.0
//     firebase_messaging: ^15.0.0
//

// ignore_for_file: avoid_print

import 'dart:async';
import 'dart:io' show Platform;
import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/material.dart';
import 'api_service.dart';

// ── Background message handler ────────────────────────────
// Must be a top-level function (not a class method) so that
// firebase_messaging can call it from a separate isolate.
@pragma('vm:entry-point')
Future<void> _fcmBackgroundHandler(RemoteMessage message) async {
  // Firebase must be initialized in the background isolate too.
  await Firebase.initializeApp();
  // No UI work here — the OS shows the notification automatically.
  // We just ensure Firebase is ready for any follow-up calls.
  print('[FCM background] type=${message.data["type"]}');
}

// ── Global navigator key ──────────────────────────────────
// Injected by main.dart so FcmService can push routes from
// outside the widget tree (e.g. when a notification is tapped
// while the app is in the background).
GlobalKey<NavigatorState>? _navigatorKey;

void setFcmNavigatorKey(GlobalKey<NavigatorState> key) {
  _navigatorKey = key;
}

// ═══════════════════════════════════════════════════════════
// FCM SERVICE SINGLETON
// ═══════════════════════════════════════════════════════════

class FcmService {
  FcmService._();
  static final FcmService instance = FcmService._();

  final FirebaseMessaging _messaging = FirebaseMessaging.instance;

  bool _initialized = false;
  String? _currentToken;

  // Stream for in-app alarm events so any listening widget can react.
  final _alarmStreamController =
      StreamController<Map<String, dynamic>>.broadcast();
  Stream<Map<String, dynamic>> get onAlarmMessage =>
      _alarmStreamController.stream;

  // ── Init ──────────────────────────────────────────────────

  /// Call once from main() after Firebase.initializeApp().
  Future<void> initialize() async {
    if (_initialized) return;
    _initialized = true;

    // 1. Request permission
    await _requestPermission();

    // 2. Android notification channel
    if (Platform.isAndroid) {
      await _messaging.setForegroundNotificationPresentationOptions(
        alert: true,
        badge: true,
        sound: true,
      );
    }

    // 3. Background handler (must be registered before any other listener)
    FirebaseMessaging.onBackgroundMessage(_fcmBackgroundHandler);

    // 4. Foreground messages
    FirebaseMessaging.onMessage.listen(_onForegroundMessage);

    // 5. Notification tap while app is in background
    FirebaseMessaging.onMessageOpenedApp.listen(_onMessageTapped);

    // 6. Notification tap while app was terminated
    final initial = await _messaging.getInitialMessage();
    if (initial != null) {
      // Slight delay so the widget tree is mounted before we push a route.
      Future.delayed(const Duration(milliseconds: 500), () {
        _onMessageTapped(initial);
      });
    }

    // 7. Register token with server
    await _refreshToken();

    // 8. Listen for token rotation
    _messaging.onTokenRefresh.listen(_onTokenRefresh);
  }

  // ── Permission ────────────────────────────────────────────

  Future<void> _requestPermission() async {
    try {
      final settings = await _messaging.requestPermission(
        alert:         true,
        announcement:  false,
        badge:         true,
        carPlay:       false,
        criticalAlert: true,   // iOS critical alerts bypass Do Not Disturb
        provisional:   false,
        sound:         true,
      );
      print('[FCM] Permission: ${settings.authorizationStatus.name}');
    } catch (e) {
      print('[FCM] Permission error: $e');
    }
  }

  // ── Token management ──────────────────────────────────────

  Future<void> _refreshToken() async {
    try {
      final token = await _messaging.getToken();
      if (token != null && token != _currentToken) {
        _currentToken = token;
        await _registerTokenWithServer(token);
      }
    } catch (e) {
      print('[FCM] Token refresh error: $e');
    }
  }

  void _onTokenRefresh(String token) {
    print('[FCM] Token rotated: ${token.substring(0, 20)}…');
    _currentToken = token;
    _registerTokenWithServer(token);
  }

  Future<void> _registerTokenWithServer(String token) async {
    try {
      await ApiService.registerFcmToken(token);
      print('[FCM] Token registered with server');
    } catch (e) {
      print('[FCM] Server registration failed: $e');
    }
  }

  /// Call on admin logout to stop receiving alarm pushes on this device.
  Future<void> unregisterToken() async {
    if (_currentToken == null) return;
    try {
      await ApiService.unregisterFcmToken(_currentToken!);
      print('[FCM] Token unregistered');
    } catch (e) {
      print('[FCM] Unregister failed: $e');
    }
  }

  // ── Message handlers ──────────────────────────────────────

  void _onForegroundMessage(RemoteMessage message) {
    print('[FCM foreground] type=${message.data["type"]}');
    _handleAlarmData(message.data);
  }

  void _onMessageTapped(RemoteMessage message) {
    print('[FCM tapped] type=${message.data["type"]}');
    _routeToAlarmPage(message.data);
  }

  void _handleAlarmData(Map<String, dynamic> data) {
    final type = data['type'] as String?;
    if (type == 'alarm_triggered' || type == 'alarm_cleared') {
      _alarmStreamController.add(Map<String, dynamic>.from(data));
    }
  }

  void _routeToAlarmPage(Map<String, dynamic> data) {
    final type = data['type'] as String?;
    if (type != 'alarm_triggered') return;

    final nav = _navigatorKey?.currentState;
    if (nav == null) return;

    // Parse mismatch list from the FCM data payload.
    // The server serialises it as a JSON string.
    List<dynamic> mismatches = [];
    try {
      final raw = data['mismatches'] as String?;
      if (raw != null) {
        mismatches = List<dynamic>.from(
          (raw.isNotEmpty
              ? (raw.startsWith('[') ? raw : '[$raw]')
              : '[]'
          ).replaceAll('"', '').split('],').map((e) {
            // Fast path: just pass raw to alarm_page which handles it
            return e;
          }),
        );
      }
    } catch (_) {}

    // Push to alarm page — import is done lazily to avoid circular deps.
    // The AlarmPage is already imported by alarm_page.dart; we use
    // a named route so no direct import is needed here.
    nav.pushNamed(
      '/alarm',
      arguments: {
        'mismatches':      mismatches,
        'mismatch_count':  int.tryParse(data['mismatch_count']?.toString() ?? '0') ?? 0,
      },
    );
  }

  // ── Diagnostics ───────────────────────────────────────────

  String? get currentToken => _currentToken;
  bool    get isInitialized => _initialized;
}