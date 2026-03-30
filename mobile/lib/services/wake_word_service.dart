import 'dart:async';
import 'package:porcupine_flutter/porcupine_flutter.dart';

/// Wraps Porcupine wake-word detection.
///
/// The "jarvis" keyword is a FREE built-in — no Picovoice access key required.
/// Call [start()] to begin listening, [stop()] to pause, [dispose()] on app exit.
class WakeWordService {
  WakeWordService._();
  static final instance = WakeWordService._();

  PorcupineManager? _manager;
  final _controller = StreamController<void>.broadcast();

  /// Stream that emits whenever "Jarvis" is detected.
  Stream<void> get wakeWordStream => _controller.stream;

  bool _running = false;
  bool get isRunning => _running;

  Future<void> init() async {
    try {
      _manager = await PorcupineManager.fromBuiltInKeywords(
        // Empty access key = free built-in keywords only
        accessKey: '',
        keywords: [BuiltInKeyword.JARVIS],
        wakeWordCallback: _onWake,
        errorCallback: _onError,
      );
    } catch (e) {
      // Porcupine unavailable (emulator / no mic) — silently disable
    }
  }

  Future<void> start() async {
    if (_manager == null) return;
    try {
      await _manager!.start();
      _running = true;
    } catch (_) {}
  }

  Future<void> stop() async {
    if (_manager == null) return;
    try {
      await _manager!.stop();
      _running = false;
    } catch (_) {}
  }

  Future<void> dispose() async {
    await stop();
    await _manager?.delete();
    _manager = null;
    await _controller.close();
  }

  void _onWake(int _) {
    if (!_controller.isClosed) _controller.add(null);
  }

  void _onError(PorcupineException e) {
    // Non-fatal — keep running
  }
}
