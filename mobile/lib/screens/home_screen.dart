import 'dart:async';
import 'package:flutter/material.dart';
import 'package:permission_handler/permission_handler.dart';
import '../services/api_service.dart';
import '../services/prefs_service.dart';
import '../services/stt_service.dart';
import '../services/tts_service.dart';
import '../services/wake_word_service.dart';
import '../theme.dart';
import '../widgets/orb_widget.dart';
import '../widgets/transcript_widget.dart';
import '../widgets/waveform_widget.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> with WidgetsBindingObserver {
  JarvisState _state = JarvisState.idle;
  String _userText = '';
  String _agentText = '';
  String _modelBadge = '';
  String _errorText = '';
  StreamSubscription<void>? _wakeSub;

  // ── Lifecycle ─────────────────────────────────────────────────────────────

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _bootstrap();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _wakeSub?.cancel();
    WakeWordService.instance.dispose();
    TtsService.instance.stop();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      WakeWordService.instance.start();
    } else if (state == AppLifecycleState.paused) {
      WakeWordService.instance.stop();
    }
  }

  // ── Bootstrap ─────────────────────────────────────────────────────────────

  Future<void> _bootstrap() async {
    // Request microphone permission
    final status = await Permission.microphone.request();
    if (!status.isGranted) {
      _setError('Microphone permission denied. Tap to retry.');
      return;
    }

    await TtsService.instance.init();
    await SttService.instance.init();
    await WakeWordService.instance.init();
    await WakeWordService.instance.start();

    _wakeSub = WakeWordService.instance.wakeWordStream.listen((_) {
      if (_state == JarvisState.idle) _startListening();
    });
  }

  // ── State machine ─────────────────────────────────────────────────────────

  Future<void> _startListening() async {
    if (_state != JarvisState.idle) return;
    _setError('');
    _setState(JarvisState.wakeDetected);

    // Brief visual flash then go to listening
    await Future.delayed(const Duration(milliseconds: 600));
    _setState(JarvisState.listening);

    await WakeWordService.instance.stop(); // release mic for STT

    final transcript = await SttService.instance.listen(
      timeout: const Duration(seconds: 10),
    );

    await WakeWordService.instance.start(); // restart wake word

    if (transcript.trim().isEmpty) {
      _setState(JarvisState.idle);
      return;
    }

    setState(() => _userText = transcript);
    await _processQuery(transcript);
  }

  Future<void> _processQuery(String query) async {
    _setState(JarvisState.processing);
    try {
      final response = await ApiService.instance.chat(query);
      setState(() {
        _agentText = response.response;
        _modelBadge = response.modelUsed;
      });
      _setState(JarvisState.speaking);
      await TtsService.instance.speak(response.response);
    } on AuthException {
      _setError('Auth failed — check password in Settings.');
    } catch (e) {
      _setError('Error: ${e.toString().substring(0, 80)}');
    } finally {
      _setState(JarvisState.idle);
    }
  }

  // ── Orb interactions ──────────────────────────────────────────────────────

  void _onOrbTap() {
    if (_state == JarvisState.idle) {
      _startListening();
    }
  }

  void _onOrbLongPress() {
    TtsService.instance.stop();
    SttService.instance.cancel();
    _setError('');
    _setState(JarvisState.idle);
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  void _setState(JarvisState s) {
    if (mounted) setState(() => _state = s);
  }

  void _setError(String msg) {
    if (mounted) setState(() => _errorText = msg);
  }

  String get _statusLabel {
    switch (_state) {
      case JarvisState.idle:         return 'JARVIS READY';
      case JarvisState.wakeDetected: return 'LISTENING…';
      case JarvisState.listening:    return 'LISTENING…';
      case JarvisState.processing:   return 'PROCESSING…';
      case JarvisState.speaking:     return 'RESPONDING…';
    }
  }

  // ── Build ─────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: kDarkBg,
      body: SafeArea(
        child: Column(
          children: [
            // ── Top bar ──────────────────────────────────────────────────────
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  Text(
                    'J · A · R · V · I · S',
                    style: Theme.of(context).textTheme.labelSmall?.copyWith(
                          letterSpacing: 5,
                          fontSize: 12,
                        ),
                  ),
                  IconButton(
                    icon: const Icon(Icons.settings_outlined, color: kCyanDim, size: 20),
                    onPressed: () => Navigator.pushNamed(context, '/settings'),
                    tooltip: 'Settings',
                  ),
                ],
              ),
            ),

            const Spacer(),

            // ── Orb ──────────────────────────────────────────────────────────
            OrbWidget(
              state: _state,
              onTap: _onOrbTap,
              onLongPress: _onOrbLongPress,
            ),

            const SizedBox(height: 24),

            // ── Waveform ─────────────────────────────────────────────────────
            WaveformWidget(state: _state),

            const SizedBox(height: 20),

            // ── Status text ──────────────────────────────────────────────────
            AnimatedSwitcher(
              duration: const Duration(milliseconds: 300),
              child: Text(
                _statusLabel,
                key: ValueKey(_statusLabel),
                style: Theme.of(context).textTheme.displayMedium,
              ),
            ),

            const SizedBox(height: 8),

            // ── Error text ───────────────────────────────────────────────────
            if (_errorText.isNotEmpty)
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 32),
                child: Text(
                  _errorText,
                  textAlign: TextAlign.center,
                  style: const TextStyle(color: Colors.redAccent, fontSize: 12),
                ),
              ),

            const Spacer(),

            // ── Transcript ───────────────────────────────────────────────────
            TranscriptWidget(
              userText: _userText,
              agentText: _agentText,
              modelBadge: _modelBadge,
            ),

            const SizedBox(height: 16),

            // ── Bottom hint ───────────────────────────────────────────────────
            Text(
              _state == JarvisState.idle
                  ? 'Say "Jarvis" or tap orb'
                  : 'Long-press orb to cancel',
              style: Theme.of(context).textTheme.labelSmall?.copyWith(fontSize: 10),
            ),

            const SizedBox(height: 20),
          ],
        ),
      ),
    );
  }
}
