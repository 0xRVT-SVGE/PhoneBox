import 'package:flutter/material.dart';
import 'package:firebase_core/firebase_core.dart';
import 'scan_page.dart';
import 'fcm_service.dart';

// Global navigator key — injected into FcmService so it can push
// the AlarmPage when a notification tap wakes the app.
final _navigatorKey = GlobalKey<NavigatorState>();

void main() async {
  // Required before any async work in main()
  WidgetsFlutterBinding.ensureInitialized();

  // Opt #34: initialize Firebase before the widget tree
  await Firebase.initializeApp();

  // Register the navigator key so FcmService can route tapped
  // notifications to the correct page.
  setFcmNavigatorKey(_navigatorKey);

  // Initialize FCM — requests permission, fetches token, registers
  // with server, and wires up background/tap handlers.
  // Non-fatal: if Firebase is not configured the app starts normally.
  try {
    await FcmService.instance.initialize();
  } catch (e) {
    // FCM misconfigured (e.g. missing google-services.json) — ignore
    debugPrint('[FCM] init skipped: $e');
  }

  runApp(FaceScanApp(navigatorKey: _navigatorKey));
}

class FaceScanApp extends StatelessWidget {
  final GlobalKey<NavigatorState> navigatorKey;

  const FaceScanApp({super.key, required this.navigatorKey});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title:                    'Face + Barcode Scanner',
      theme:                    ThemeData.dark(useMaterial3: true),
      navigatorKey:             navigatorKey,
      debugShowCheckedModeBanner: false,
      // Named routes — used by FcmService._routeToAlarmPage()
      initialRoute: '/',
      routes: {
        '/': (_) => const ScanPage(),
        // '/alarm' is pushed programmatically with arguments by FcmService;
        // it is handled in ScanPage's _handleAlarmTriggered listener and
        // does not need a static route entry.
      },
    );
  }
}