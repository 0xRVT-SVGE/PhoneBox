// front_end/shimmer_widgets.dart
// Opt #31 — Skeleton loaders using the shimmer package.
//
// Add to pubspec.yaml:  shimmer: ^3.0.0
// Then run: flutter pub get
//
// Usage — replace any loading spinner with the appropriate skeleton:
//
//   // Student / phone list page while _loading == true:
//   _loading ? PhoneListSkeleton() : ListView.builder(...)
//
//   // Single card while data is fetching:
//   _loading ? PhoneCardSkeleton() : _buildPhoneCard(phone)

import 'package:flutter/material.dart';
import 'package:shimmer/shimmer.dart';

// ── Shared shimmer base colour ────────────────────────────────────────────────
// Dark-theme colours that match the app's Color(0xFF1C1C1E) card background.
const _kBaseColor      = Color(0xFF2C2C2E);
const _kHighlightColor = Color(0xFF3A3A3C);


// ── Generic shimmer box ───────────────────────────────────────────────────────

class _ShimmerBox extends StatelessWidget {
  final double width;
  final double height;
  final double borderRadius;

  const _ShimmerBox({
    required this.width,
    required this.height,
    this.borderRadius = 6,
  });

  @override
  Widget build(BuildContext context) => Container(
        width:  width,
        height: height,
        decoration: BoxDecoration(
          color:        _kBaseColor,
          borderRadius: BorderRadius.circular(borderRadius),
        ),
      );
}


// ── Single phone card skeleton ────────────────────────────────────────────────

class PhoneCardSkeleton extends StatelessWidget {
  const PhoneCardSkeleton({super.key});

  @override
  Widget build(BuildContext context) => Shimmer.fromColors(
        baseColor:      _kBaseColor,
        highlightColor: _kHighlightColor,
        child: Card(
          margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
          color:  const Color(0xFF1C1C1E),
          child:  Padding(
            padding: const EdgeInsets.symmetric(vertical: 12, horizontal: 14),
            child: Row(children: [
              // Left: text lines
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const _ShimmerBox(width: 140, height: 14),
                    const SizedBox(height: 8),
                    const _ShimmerBox(width: 100, height: 11),
                    const SizedBox(height: 6),
                    const _ShimmerBox(width: 80,  height: 11),
                    const SizedBox(height: 6),
                    const _ShimmerBox(width: 60,  height: 11),
                  ],
                ),
              ),
              const SizedBox(width: 12),
              // Right: two action button placeholders
              Column(children: const [
                _ShimmerBox(width: 80, height: 36, borderRadius: 8),
                SizedBox(height: 8),
                _ShimmerBox(width: 80, height: 36, borderRadius: 8),
              ]),
            ]),
          ),
        ),
      );
}


// ── Phone list skeleton (N cards) ─────────────────────────────────────────────

class PhoneListSkeleton extends StatelessWidget {
  final int count;
  const PhoneListSkeleton({super.key, this.count = 4});

  @override
  Widget build(BuildContext context) => ListView.builder(
        itemCount:   count,
        itemBuilder: (_, __) => const PhoneCardSkeleton(),
      );
}


// ── Student card skeleton ─────────────────────────────────────────────────────

class StudentCardSkeleton extends StatelessWidget {
  const StudentCardSkeleton({super.key});

  @override
  Widget build(BuildContext context) => Shimmer.fromColors(
        baseColor:      _kBaseColor,
        highlightColor: _kHighlightColor,
        child: Card(
          margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
          color:  const Color(0xFF1C1C1E),
          child:  ListTile(
            title:    const _ShimmerBox(width: 120, height: 13),
            subtitle: Padding(
              padding: const EdgeInsets.only(top: 6),
              child:   const _ShimmerBox(width: 80, height: 11),
            ),
            trailing: Row(mainAxisSize: MainAxisSize.min, children: const [
              _ShimmerBox(width: 32, height: 32, borderRadius: 16),
              SizedBox(width: 6),
              _ShimmerBox(width: 32, height: 32, borderRadius: 16),
              SizedBox(width: 6),
              _ShimmerBox(width: 32, height: 32, borderRadius: 16),
            ]),
          ),
        ),
      );
}


// ── Student list skeleton ─────────────────────────────────────────────────────

class StudentListSkeleton extends StatelessWidget {
  final int count;
  const StudentListSkeleton({super.key, this.count = 6});

  @override
  Widget build(BuildContext context) => ListView.builder(
        itemCount:   count,
        itemBuilder: (_, __) => const StudentCardSkeleton(),
      );
}


// ── Alarm mismatch list skeleton ──────────────────────────────────────────────

class MismatchListSkeleton extends StatelessWidget {
  final int count;
  const MismatchListSkeleton({super.key, this.count = 3});

  @override
  Widget build(BuildContext context) => Shimmer.fromColors(
        baseColor:      _kBaseColor,
        highlightColor: _kHighlightColor,
        child: Column(
          children: List.generate(count, (_) => Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
            child: Row(children: [
              const _ShimmerBox(width: 34, height: 34, borderRadius: 8),
              const SizedBox(width: 12),
              Column(crossAxisAlignment: CrossAxisAlignment.start, children: const [
                _ShimmerBox(width: 130, height: 13),
                SizedBox(height: 6),
                _ShimmerBox(width: 90,  height: 11),
              ]),
            ]),
          )),
        ),
      );
}


// ── Admin phone list skeleton (stored + taken sections) ───────────────────────

class AdminPhoneListSkeleton extends StatelessWidget {
  final int storedCount;
  final int takenCount;
  const AdminPhoneListSkeleton({
    super.key,
    this.storedCount = 3,
    this.takenCount  = 2,
  });

  @override
  Widget build(BuildContext context) => ListView(
        children: [
          // Section header placeholder
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 16, 16, 4),
            child: Shimmer.fromColors(
              baseColor: _kBaseColor, highlightColor: _kHighlightColor,
              child: const _ShimmerBox(width: 160, height: 13),
            ),
          ),
          ...List.generate(storedCount, (_) => const PhoneCardSkeleton()),
          const SizedBox(height: 8),
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 4),
            child: Shimmer.fromColors(
              baseColor: _kBaseColor, highlightColor: _kHighlightColor,
              child: const _ShimmerBox(width: 140, height: 13),
            ),
          ),
          ...List.generate(takenCount, (_) => const PhoneCardSkeleton()),
        ],
      );
}