import 'dart:convert';
import 'package:http/http.dart' as http;
import '../models/chat_response.dart';
import 'prefs_service.dart';

class AuthException implements Exception {
  const AuthException();
  @override
  String toString() => 'Unauthorized — check your X-Token password in Settings.';
}

class ApiService {
  ApiService._();
  static final instance = ApiService._();

  static const _timeout = Duration(seconds: 30);

  String get _base => PrefsService.instance.serverUrl.trimRight().replaceAll(RegExp(r'/$'), '');
  Map<String, String> get _headers => {
        'Content-Type': 'application/json',
        'X-Token': PrefsService.instance.xToken,
      };

  /// Send a message to Super Agent's /chat endpoint.
  Future<ChatResponse> chat(String message) async {
    final uri = Uri.parse('$_base/chat');
    final body = jsonEncode({
      'message': message,
      'session_id': PrefsService.instance.sessionId,
    });

    final resp = await http
        .post(uri, headers: _headers, body: body)
        .timeout(_timeout);

    if (resp.statusCode == 401) throw const AuthException();
    if (resp.statusCode != 200) {
      throw Exception('Super Agent error ${resp.statusCode}: ${resp.body}');
    }

    final json = jsonDecode(resp.body) as Map<String, dynamic>;
    return ChatResponse.fromJson(json);
  }

  /// Validate credentials. Returns true if server accepts the token.
  Future<bool> testAuth() async {
    try {
      final uri = Uri.parse('$_base/auth');
      final body = jsonEncode({'password': PrefsService.instance.xToken});
      final resp = await http
          .post(uri,
              headers: {'Content-Type': 'application/json'},
              body: body)
          .timeout(_timeout);
      if (resp.statusCode == 200) {
        final json = jsonDecode(resp.body) as Map<String, dynamic>;
        return json['ok'] == true;
      }
      return false;
    } catch (_) {
      return false;
    }
  }

  /// Health check — returns true if the server is reachable.
  Future<bool> ping() async {
    try {
      final uri = Uri.parse('$_base/health');
      final resp = await http.get(uri).timeout(const Duration(seconds: 8));
      return resp.statusCode == 200;
    } catch (_) {
      return false;
    }
  }
}
