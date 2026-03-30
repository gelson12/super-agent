import 'package:flutter/material.dart';
import '../services/api_service.dart';
import '../services/prefs_service.dart';
import '../theme.dart';

class SettingsScreen extends StatefulWidget {
  final bool isFirstLaunch;
  const SettingsScreen({super.key, required this.isFirstLaunch});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late final TextEditingController _urlCtrl;
  late final TextEditingController _tokenCtrl;
  late final TextEditingController _elKeyCtrl;
  late final TextEditingController _elVoiceCtrl;

  bool _obscureToken = true;
  bool _obscureElKey = true;
  bool _testing = false;
  String? _testResult;

  @override
  void initState() {
    super.initState();
    final p = PrefsService.instance;
    _urlCtrl = TextEditingController(text: p.serverUrl);
    _tokenCtrl = TextEditingController(text: p.xToken);
    _elKeyCtrl = TextEditingController(text: p.elevenLabsKey);
    _elVoiceCtrl = TextEditingController(text: p.elevenLabsVoiceId);
  }

  @override
  void dispose() {
    _urlCtrl.dispose();
    _tokenCtrl.dispose();
    _elKeyCtrl.dispose();
    _elVoiceCtrl.dispose();
    super.dispose();
  }

  Future<void> _testConnection() async {
    setState(() { _testing = true; _testResult = null; });
    await _save(silent: true);
    final ok = await ApiService.instance.testAuth();
    setState(() {
      _testing = false;
      _testResult = ok ? '✓ Connected' : '✗ Failed — check URL and password';
    });
  }

  Future<void> _save({bool silent = false}) async {
    final p = PrefsService.instance;
    await p.setServerUrl(_urlCtrl.text);
    await p.setXToken(_tokenCtrl.text);
    await p.setElevenLabsKey(_elKeyCtrl.text);
    await p.setElevenLabsVoiceId(_elVoiceCtrl.text);

    if (!silent && mounted) {
      if (widget.isFirstLaunch) {
        Navigator.pushReplacementNamed(context, '/home');
      } else {
        Navigator.pop(context);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: kDarkBg,
      appBar: AppBar(
        title: const Text('SETTINGS'),
        leading: widget.isFirstLaunch
            ? null
            : IconButton(
                icon: const Icon(Icons.arrow_back_ios, color: kCyanDim, size: 18),
                onPressed: () => Navigator.pop(context),
              ),
      ),
      body: ListView(
        padding: const EdgeInsets.all(24),
        children: [
          // ── Server section ──────────────────────────────────────────────
          _sectionLabel(context, 'SUPER AGENT SERVER'),
          const SizedBox(height: 12),
          TextField(
            controller: _urlCtrl,
            decoration: const InputDecoration(
              labelText: 'Server URL',
              hintText: 'https://your-service.railway.app',
            ),
            keyboardType: TextInputType.url,
            autocorrect: false,
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _tokenCtrl,
            obscureText: _obscureToken,
            decoration: InputDecoration(
              labelText: 'Password / X-Token',
              suffixIcon: IconButton(
                icon: Icon(
                  _obscureToken ? Icons.visibility_off : Icons.visibility,
                  color: kCyanDim,
                  size: 18,
                ),
                onPressed: () => setState(() => _obscureToken = !_obscureToken),
              ),
            ),
          ),
          const SizedBox(height: 16),
          Row(
            children: [
              ElevatedButton(
                onPressed: _testing ? null : _testConnection,
                child: _testing
                    ? const SizedBox(
                        width: 14, height: 14,
                        child: CircularProgressIndicator(strokeWidth: 1.5, color: kCyan))
                    : const Text('Test Connection'),
              ),
              if (_testResult != null) ...[
                const SizedBox(width: 16),
                Flexible(
                  child: Text(
                    _testResult!,
                    style: TextStyle(
                      color: _testResult!.startsWith('✓') ? kCyan : Colors.redAccent,
                      fontSize: 12,
                    ),
                  ),
                ),
              ],
            ],
          ),

          const SizedBox(height: 32),

          // ── Voice section ───────────────────────────────────────────────
          _sectionLabel(context, 'VOICE — ELEVENLABS (OPTIONAL)'),
          const SizedBox(height: 8),
          Text(
            'Leave blank to use the built-in British TTS fallback.\n'
            'Free tier: 10,000 characters/month.',
            style: Theme.of(context).textTheme.labelSmall,
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _elKeyCtrl,
            obscureText: _obscureElKey,
            decoration: InputDecoration(
              labelText: 'ElevenLabs API Key',
              hintText: 'xi-...',
              suffixIcon: IconButton(
                icon: Icon(
                  _obscureElKey ? Icons.visibility_off : Icons.visibility,
                  color: kCyanDim,
                  size: 18,
                ),
                onPressed: () => setState(() => _obscureElKey = !_obscureElKey),
              ),
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _elVoiceCtrl,
            decoration: const InputDecoration(
              labelText: 'Voice ID',
              hintText: 'pNInz6obpgDQGcFmaJgB  (Adam — default)',
            ),
            autocorrect: false,
          ),
          const SizedBox(height: 8),
          Text(
            'Adam voice ID: pNInz6obpgDQGcFmaJgB\n'
            'Find more voices at elevenlabs.io/voice-library',
            style: Theme.of(context).textTheme.labelSmall,
          ),

          const SizedBox(height: 40),

          // ── Save button ─────────────────────────────────────────────────
          ElevatedButton(
            onPressed: () => _save(),
            style: ElevatedButton.styleFrom(
              padding: const EdgeInsets.symmetric(vertical: 14),
            ),
            child: Text(
              widget.isFirstLaunch ? 'CONNECT' : 'SAVE',
              style: const TextStyle(letterSpacing: 4, fontSize: 13),
            ),
          ),
        ],
      ),
    );
  }

  Widget _sectionLabel(BuildContext context, String label) {
    return Text(label, style: Theme.of(context).textTheme.labelSmall?.copyWith(letterSpacing: 3));
  }
}
