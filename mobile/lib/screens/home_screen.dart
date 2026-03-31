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
  StreamSubscription<String>? _partialSub;

  // Flag set to true when the user cancels a processing request
  bool _processingCancelled = false;

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
    _partialSub?.cancel();
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
      if (_state == JarvisState.idle) {
        _startListening();
      } else if (_state == JarvisState.speaking) {
        // Wake word interrupts speech → immediately start listening
        _interrupt(thenListen: true);
      }
    });
  }

  // ── State machine ─────────────────────────────────────────────────────────

  Future<void> _startListening() async {
    if (_state != JarvisState.idle) return;
    _setError('');
    _setState(JarvisState.wakeDetected);

    await Future.delayed(const Duration(milliseconds: 500));
    _setState(JarvisState.listening);

    await WakeWordService.instance.stop(); // release mic for STT

    // Show partial results in real-time as the user speaks
    _partialSub?.cancel();
    _partialSub = SttService.instance.partialResults.listen((partial) {
      if (mounted && _state == JarvisState.listening) {
        setState(() => _userText = partial);
      }
    });

    final transcript = await SttService.instance.listen(
      timeout: const Duration(seconds: 10),
    );

    _partialSub?.cancel();
    _partialSub = null;

    await WakeWordService.instance.start();

    if (transcript.trim().isEmpty) {
      _setState(JarvisState.idle);
      return;
    }

    setState(() => _userText = transcript);
    await _processQuery(transcript);
  }

  Future<void> _processQuery(String query) async {
    _processingCancelled = false;
    _setState(JarvisState.processing);
    try {
      final response = await ApiService.instance.chat(query);

      // User cancelled while API was in flight — discard response
      if (_processingCancelled || !mounted) {
        _setState(JarvisState.idle);
        return;
      }

      setState(() {
        _agentText = response.response;
        _modelBadge = response.modelUsed;
      });
      _setState(JarvisState.speaking);
      await TtsService.instance.speak(response.response);

    } on AuthException {
      _setError('Auth failed — check password in Settings.');
    } catch (e) {
      if (!_processingCancelled) {
        final msg = e.toString();
        _setError('Error: ${msg.length > 120 ? msg.substring(0, 120) : msg}');
      }
    } finally {
      if (mounted) _setState(JarvisState.idle);
    }
  }

  // ── Interruption ──────────────────────────────────────────────────────────

  /// Stop TTS immediately. If [thenListen] is true, re-enter listening state.
  Future<void> _interrupt({bool thenListen = false}) async {
    await TtsService.instance.stop();
    if (!mounted) return;
    // Brief wake-flash so user knows the interrupt was registered
    _setState(JarvisState.wakeDetected);
    await Future.delayed(const Duration(milliseconds: 300));
    _setState(JarvisState.idle);
    if (thenListen) _startListening();
  }

  /// Cancel an in-flight API request.
  void _cancelProcessing() {
    _processingCancelled = true;
    _setError('');
    _setState(JarvisState.idle);
  }

  // ── Orb interactions ──────────────────────────────────────────────────────

  void _onOrbTap() {
    switch (_state) {
      case JarvisState.idle:
        // Manually start listening
        _startListening();
        break;
      case JarvisState.speaking:
        // Tap during speech → interrupt and immediately listen
        _interrupt(thenListen: true);
        break;
      case JarvisState.processing:
        // Tap during API call → cancel it
        _cancelProcessing();
        break;
      default:
        break;
    }
  }

  void _onOrbLongPress() {
    // Hard cancel — stop everything, return to idle
    _processingCancelled = true;
    TtsService.instance.stop();
    SttService.instance.cancel();
    _partialSub?.cancel();
    _partialSub = null;
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

  String get _hintLabel {
    switch (_state) {
      case JarvisState.idle:
        return 'Say "Jarvis" or tap orb';
      case JarvisState.speaking:
        return 'Tap orb to interrupt';
      case JarvisState.processing:
        return 'Tap orb to cancel';
      default:
        return 'Long-press orb to cancel';
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
            AnimatedSwitcher(
              duration: const Duration(milliseconds: 200),
              child: Text(
                _hintLabel,
                key: ValueKey(_hintLabel),
                style: Theme.of(context).textTheme.labelSmall?.copyWith(
                      fontSize: 10,
                      color: _state == JarvisState.speaking
                          ? kCyan.withOpacity(0.7) // highlight interrupt hint
                          : null,
                    ),
              ),
            ),

            const SizedBox(height: 20),
          ],
        ),
      ),
    );
  }
}
