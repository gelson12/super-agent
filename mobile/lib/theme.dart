import 'package:flutter/material.dart';

// ── JARVIS colour palette ─────────────────────────────────────────────────────
const kCyan = Color(0xFF00E5FF);
const kCyanDim = Color(0xFF0097A7);
const kBlue = Color(0xFF0091EA);
const kDarkBg = Color(0xFF000000);
const kSurfaceBg = Color(0xFF0A0A12);
const kTextPrimary = Color(0xFFE0F7FA);
const kTextSecondary = Color(0xFF80CBC4);

// ── Glow helpers ──────────────────────────────────────────────────────────────
BoxShadow cyanGlow({double spread = 8, double blur = 24, double opacity = 0.6}) =>
    BoxShadow(
      color: kCyan.withOpacity(opacity),
      spreadRadius: spread,
      blurRadius: blur,
    );

// ── Theme ─────────────────────────────────────────────────────────────────────
final jarvisTheme = ThemeData(
  brightness: Brightness.dark,
  scaffoldBackgroundColor: kDarkBg,
  colorScheme: const ColorScheme.dark(
    primary: kCyan,
    secondary: kBlue,
    surface: kSurfaceBg,
    onPrimary: kDarkBg,
    onSurface: kTextPrimary,
  ),
  appBarTheme: const AppBarTheme(
    backgroundColor: kDarkBg,
    foregroundColor: kCyan,
    elevation: 0,
    titleTextStyle: TextStyle(
      color: kCyan,
      fontSize: 14,
      letterSpacing: 6,
      fontWeight: FontWeight.w300,
    ),
  ),
  textTheme: const TextTheme(
    displayLarge: TextStyle(color: kCyan, fontSize: 28, letterSpacing: 8, fontWeight: FontWeight.w200),
    displayMedium: TextStyle(color: kTextPrimary, fontSize: 18, letterSpacing: 4, fontWeight: FontWeight.w300),
    bodyMedium: TextStyle(color: kTextSecondary, fontSize: 13, height: 1.5),
    labelSmall: TextStyle(color: kCyanDim, fontSize: 11, letterSpacing: 2),
  ),
  elevatedButtonTheme: ElevatedButtonThemeData(
    style: ElevatedButton.styleFrom(
      backgroundColor: kSurfaceBg,
      foregroundColor: kCyan,
      side: const BorderSide(color: kCyan, width: 1),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(4)),
    ),
  ),
  inputDecorationTheme: const InputDecorationTheme(
    filled: true,
    fillColor: kSurfaceBg,
    border: OutlineInputBorder(borderSide: BorderSide(color: kCyanDim)),
    enabledBorder: OutlineInputBorder(borderSide: BorderSide(color: kCyanDim)),
    focusedBorder: OutlineInputBorder(borderSide: BorderSide(color: kCyan, width: 2)),
    labelStyle: TextStyle(color: kCyanDim),
    hintStyle: TextStyle(color: Color(0xFF37474F)),
  ),
);
