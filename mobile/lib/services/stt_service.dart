import 'dart:async';
import 'package:speech_to_text/speech_to_text.dart';

/// Wraps the speech_to_text package (Android SpeechRecognizer).
/// Free, on-device, no API cost.
class SttService {
  SttService._();
  static final instance = SttService._();

  final _stt = SpeechToText();
  bool _initialized = false;

  final _partialController = StreamController<String>.broadcast();

  /// Stream of partial recognition results (updates as user speaks).
  Stream<String> get partialResults => _partialController.stream;

  String _lastResult = '';
  String get lastResult => _lastResult;

  Future<bool> init() async {
    if (_initialized) return true;
    _initialized = await _stt.initialize(
      onError: (_) {},
      onStatus: (_) {},
    );
    return _initialized;
  }

  bool get isListening => _stt.isListening;

  /// Start listening. Completes with the final recognised text, or empty
  /// string on silence timeout / cancellation.
  Future<String> listen({Duration timeout = const Duration(seconds: 10)}) async {
    _lastResult = '';

    if (!_initialized) await init();
    if (!_initialized) return '';

    final completer = Completer<String>();

    await _stt.listen(
      listenFor: timeout,
      pauseFor: const Duration(seconds: 3),
      partialResults: true,
      onResult: (result) {
        final text = result.recognizedWords;
        if (text.isNotEmpty) {
          _lastResult = text;
          if (!_partialController.isClosed) _partialController.add(text);
        }
        if (result.finalResult && !completer.isCompleted) {
          completer.complete(text);
        }
      },
    );

    // Safety timeout
    return Future.any([
      completer.future,
      Future.delayed(timeout + const Duration(seconds: 1), () => _lastResult),
    ]);
  }

  Future<void> stop() async {
    await _stt.stop();
  }

  Future<void> cancel() async {
    await _stt.cancel();
    _lastResult = '';
  }
}
