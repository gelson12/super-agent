import 'dart:io';
import 'dart:typed_data';
import 'package:flutter_tts/flutter_tts.dart';
import 'package:http/http.dart' as http;
import 'package:just_audio/just_audio.dart';
import 'package:path_provider/path_provider.dart';
import 'prefs_service.dart';

/// Text-to-speech service.
///
/// Primary: ElevenLabs REST API (deep JARVIS-like voice, 10k chars/month free).
/// Fallback: flutter_tts with British English accent (zero cost, always available).
class TtsService {
  TtsService._();
  static final instance = TtsService._();

  final _player = AudioPlayer();
  final _ftts = FlutterTts();
  bool _speaking = false;

  bool get isSpeaking => _speaking;

  Future<void> init() async {
    await _ftts.setLanguage('en-GB');
    await _ftts.setSpeechRate(0.42);
    await _ftts.setPitch(0.88);
    await _ftts.setVolume(1.0);
    _ftts.setCompletionHandler(() => _speaking = false);
  }

  /// Speak [text]. Uses ElevenLabs if an API key is configured, else flutter_tts.
  Future<void> speak(String text) async {
    if (text.trim().isEmpty) return;
    _speaking = true;

    final key = PrefsService.instance.elevenLabsKey;
    if (key.isNotEmpty) {
      final success = await _speakElevenLabs(text, key);
      if (success) return;
    }

    // Fallback
    await _speakFallback(text);
  }

  Future<void> stop() async {
    _speaking = false;
    try { await _player.stop(); } catch (_) {}
    try { await _ftts.stop(); } catch (_) {}
  }

  // ── ElevenLabs ──────────────────────────────────────────────────────────────

  Future<bool> _speakElevenLabs(String text, String apiKey) async {
    File? tmpFile;
    try {
      final voiceId = PrefsService.instance.elevenLabsVoiceId;
      final uri = Uri.parse(
        'https://api.elevenlabs.io/v1/text-to-speech/$voiceId',
      );

      final resp = await http.post(
        uri,
        headers: {
          'xi-api-key': apiKey,
          'Content-Type': 'application/json',
          'Accept': 'audio/mpeg',
        },
        body: '{"text":"${_escape(text)}",'
            '"model_id":"eleven_monolingual_v1",'
            '"voice_settings":{"stability":0.5,"similarity_boost":0.75}}',
      );

      if (resp.statusCode != 200) return false;

      final bytes = resp.bodyBytes as Uint8List;
      final dir = await getTemporaryDirectory();
      tmpFile = File('${dir.path}/jarvis_tts.mp3');
      await tmpFile.writeAsBytes(bytes);

      await _player.setFilePath(tmpFile.path);
      await _player.play();

      // Wait until playback completes
      await _player.processingStateStream
          .firstWhere((s) => s == ProcessingState.completed);

      _speaking = false;
      return true;
    } catch (_) {
      return false;
    } finally {
      try { tmpFile?.deleteSync(); } catch (_) {}
    }
  }

  // ── Fallback TTS ────────────────────────────────────────────────────────────

  Future<void> _speakFallback(String text) async {
    await _ftts.speak(text);
    // flutter_tts completion fires via setCompletionHandler above
  }

  String _escape(String s) => s
      .replaceAll(r'\', r'\\')
      .replaceAll('"', r'\"')
      .replaceAll('\n', ' ');
}
