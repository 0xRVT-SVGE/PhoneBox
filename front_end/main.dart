import 'package:flutter/material.dart';
import 'scan_page.dart';

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
