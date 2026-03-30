import 'package:shared_preferences/shared_preferences.dart';
import 'package:uuid/uuid.dart';

/// Persistent settings backed by SharedPreferences.
/// Call [init()] once at app start before accessing any field.
class PrefsService {
  PrefsService._();
  static final instance = PrefsService._();

  late SharedPreferences _prefs;

  static const _kServerUrl = 'server_url';
  static const _kXToken = 'x_token';
  static const _kSessionId = 'session_id';
  static const _kElevenLabsKey = 'elevenlabs_key';
  static const _kElevenLabsVoiceId = 'elevenlabs_voice_id';

  /// Default Adam voice — deep, authoritative, JARVIS-like
  static const defaultVoiceId = 'pNInz6obpgDQGcFmaJgB';

  Future<void> init() async {
    _prefs = await SharedPreferences.getInstance();
    // Generate a session ID once and keep it forever
    if (_prefs.getString(_kSessionId) == null) {
      await _prefs.setString(_kSessionId, const Uuid().v4());
    }
  }

  // ── Getters ──────────────────────────────────────────────────────────────

  String get serverUrl => _prefs.getString(_kServerUrl) ?? '';
  String get xToken => _prefs.getString(_kXToken) ?? '';
  String get sessionId => _prefs.getString(_kSessionId) ?? 'default';
  String get elevenLabsKey => _prefs.getString(_kElevenLabsKey) ?? '';
  String get elevenLabsVoiceId =>
      _prefs.getString(_kElevenLabsVoiceId) ?? defaultVoiceId;

  // ── Setters ──────────────────────────────────────────────────────────────

  Future<void> setServerUrl(String v) => _prefs.setString(_kServerUrl, v.trim());
  Future<void> setXToken(String v) => _prefs.setString(_kXToken, v.trim());
  Future<void> setElevenLabsKey(String v) => _prefs.setString(_kElevenLabsKey, v.trim());
  Future<void> setElevenLabsVoiceId(String v) =>
      _prefs.setString(_kElevenLabsVoiceId, v.trim());
}
