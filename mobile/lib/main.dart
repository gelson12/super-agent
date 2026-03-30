import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'services/prefs_service.dart';
import 'screens/home_screen.dart';
import 'screens/settings_screen.dart';
import 'theme.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // Force portrait — JARVIS HUD looks best tall
  await SystemChrome.setPreferredOrientations([
    DeviceOrientation.portraitUp,
  ]);

  // Dark immersive status bar
  SystemChrome.setSystemUIOverlayStyle(const SystemUiOverlayStyle(
    statusBarColor: Colors.transparent,
    statusBarIconBrightness: Brightness.light,
  ));

  await PrefsService.instance.init();

  runApp(const JarvisApp());
}

class JarvisApp extends StatelessWidget {
  const JarvisApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'J.A.R.V.I.S',
      theme: jarvisTheme,
      debugShowCheckedModeBanner: false,
      home: PrefsService.instance.serverUrl.isEmpty
          ? const SettingsScreen(isFirstLaunch: true)
          : const HomeScreen(),
      routes: {
        '/home': (_) => const HomeScreen(),
        '/settings': (_) => const SettingsScreen(isFirstLaunch: false),
      },
    );
  }
}
