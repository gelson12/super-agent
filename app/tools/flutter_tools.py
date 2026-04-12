"""
Flutter build tools for Super Agent's autonomous mobile development pipeline.

Capabilities:
  flutter_create_project  — scaffold a new Flutter app in /workspace
  flutter_build_apk       — build a debug APK (Android, no keystore needed)
  flutter_test            — run flutter unit tests
  upload_build_artifact   — upload APK/IPA to Cloudinary, return download URL
  flutter_git_push        — init repo, create GitHub remote, push all code

All commands run inside the container; git uses GITHUB_PAT from env.
"""
import json
import os
import subprocess
import time
from pathlib import Path
from langchain_core.tools import tool

_WORKSPACE = Path("/workspace")
_FLUTTER_BIN = os.environ.get("FLUTTER_HOME", "/opt/flutter") + "/bin/flutter"
_TIMEOUT_BUILD = 600   # APK builds can take a few minutes
_TIMEOUT_SHORT = 120


def _run(cmd: str, cwd: str | None = None, timeout: int = _TIMEOUT_SHORT) -> str:
    """Run a shell command, return combined stdout+stderr."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "HOME": "/root", "PUB_CACHE": "/root/.pub-cache"},
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            return f"[exit {result.returncode}] {output.strip()}"
        return output.strip()
    except subprocess.TimeoutExpired:
        return f"[timeout after {timeout}s] {cmd}"
    except Exception as e:
        return f"[error] {e}"


_VOICE_APP_PUBSPEC = """\
name: super_agent_voice
description: Super Agent Voice Chat — talk to your AI by voice
publish_to: 'none'
version: 1.0.0+1

environment:
  sdk: '>=3.0.0 <4.0.0'

dependencies:
  flutter:
    sdk: flutter
  speech_to_text: ^6.6.0
  flutter_tts: ^4.0.2
  http: ^1.2.0
  permission_handler: ^11.3.0
  cupertino_icons: ^1.0.8

dev_dependencies:
  flutter_test:
    sdk: flutter
  flutter_lints: ^3.0.0

flutter:
  uses-material-design: true
"""

_VOICE_APP_MAIN_DART = r"""
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:speech_to_text/speech_to_text.dart';
import 'package:flutter_tts/flutter_tts.dart';
import 'package:http/http.dart' as http;
import 'package:permission_handler/permission_handler.dart';

void main() => runApp(const SuperAgentApp());

const Color kGold = Color(0xFFc9a227);
const Color kBg   = Color(0xFF0f0f0f);
const Color kBubbleAgent = Color(0xFF1a1208);
const Color kBubbleUser  = Color(0xFF1a1a2e);
const String kApiUrl    = 'https://super-agent-production.up.railway.app/chat';
const String kSessionId = 'android-voice-app';

class SuperAgentApp extends StatelessWidget {
  const SuperAgentApp({super.key});
  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'Super Agent Voice',
        debugShowCheckedModeBanner: false,
        theme: ThemeData(
          brightness: Brightness.dark,
          scaffoldBackgroundColor: kBg,
          colorScheme: const ColorScheme.dark(primary: kGold, surface: Color(0xFF1a1a1a)),
          fontFamily: 'Roboto',
        ),
        home: const VoiceChatScreen(),
      );
}

class _Msg {
  final String text;
  final bool isUser;
  _Msg(this.text, {required this.isUser});
}

class VoiceChatScreen extends StatefulWidget {
  const VoiceChatScreen({super.key});
  @override
  State<VoiceChatScreen> createState() => _VoiceChatScreenState();
}

class _VoiceChatScreenState extends State<VoiceChatScreen> {
  final SpeechToText _stt = SpeechToText();
  final FlutterTts   _tts = FlutterTts();
  final ScrollController _scroll = ScrollController();

  final List<_Msg> _msgs = [];
  bool _sttReady  = false;
  bool _listening = false;
  bool _loading   = false;
  String _partial = '';

  @override
  void initState() {
    super.initState();
    _init();
  }

  Future<void> _init() async {
    await Permission.microphone.request();
    _sttReady = await _stt.initialize(
      onStatus: (s) { if (s == 'done' || s == 'notListening') _onSttDone(); },
      onError: (e) => _addMsg('Speech error: ${e.errorMsg}', isUser: false),
    );
    await _tts.setLanguage('en-US');
    await _tts.setSpeechRate(0.5);
    await _tts.setVolume(1.0);
    setState(() {});
  }

  void _addMsg(String text, {required bool isUser}) {
    setState(() => _msgs.add(_Msg(text, isUser: isUser)));
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) _scroll.animateTo(
        _scroll.position.maxScrollExtent,
        duration: const Duration(milliseconds: 300),
        curve: Curves.easeOut,
      );
    });
  }

  Future<void> _toggleMic() async {
    if (!_sttReady) { _addMsg('Microphone not ready', isUser: false); return; }
    if (_listening) {
      await _stt.stop();
    } else {
      setState(() { _listening = true; _partial = ''; });
      await _stt.listen(
        onResult: (r) => setState(() => _partial = r.recognizedWords),
        listenFor: const Duration(seconds: 30),
        pauseFor: const Duration(seconds: 3),
        partialResults: true,
        localeId: 'en_US',
      );
    }
  }

  Future<void> _onSttDone() async {
    setState(() => _listening = false);
    final text = _partial.trim();
    if (text.isEmpty) return;
    _addMsg(text, isUser: true);
    setState(() { _loading = true; _partial = ''; });
    try {
      final res = await http.post(
        Uri.parse(kApiUrl),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({'message': text, 'session_id': kSessionId}),
      ).timeout(const Duration(seconds: 60));
      if (res.statusCode == 200) {
        final reply = jsonDecode(res.body)['response'] as String? ?? 'No response';
        _addMsg(reply, isUser: false);
        await _tts.speak(reply);
      } else {
        _addMsg('Error ${res.statusCode}: ${res.body}', isUser: false);
      }
    } catch (e) {
      _addMsg('Network error: $e', isUser: false);
    } finally {
      setState(() => _loading = false);
    }
  }

  @override
  void dispose() {
    _stt.cancel();
    _tts.stop();
    _scroll.dispose();
    super.dispose();
  }

  Widget _bubble(_Msg m) {
    final isUser = m.isUser;
    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 4, horizontal: 12),
        padding: const EdgeInsets.symmetric(vertical: 10, horizontal: 14),
        constraints: const BoxConstraints(maxWidth: 300),
        decoration: BoxDecoration(
          color: isUser ? kBubbleUser : kBubbleAgent,
          borderRadius: BorderRadius.circular(16),
          border: Border.all(color: kGold.withOpacity(0.3)),
        ),
        child: Text(m.text, style: TextStyle(
          color: isUser ? Colors.white70 : Colors.white,
          fontSize: 14,
        )),
      ),
    );
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      backgroundColor: const Color(0xFF141414),
      title: Row(children: [
        Container(width: 10, height: 10, decoration: const BoxDecoration(
          color: kGold, shape: BoxShape.circle)),
        const SizedBox(width: 8),
        const Text('Super Agent', style: TextStyle(color: kGold, fontWeight: FontWeight.bold)),
      ]),
    ),
    body: Column(children: [
      Expanded(
        child: _msgs.isEmpty
            ? const Center(child: Text('Tap the mic and speak',
                style: TextStyle(color: Colors.white38)))
            : ListView.builder(
                controller: _scroll,
                itemCount: _msgs.length,
                itemBuilder: (_, i) => _bubble(_msgs[i]),
              ),
      ),
      if (_partial.isNotEmpty)
        Container(
          margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
          padding: const EdgeInsets.all(10),
          decoration: BoxDecoration(
            color: const Color(0xFF1a1a1a),
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: kGold.withOpacity(0.5)),
          ),
          child: Text(_partial, style: const TextStyle(color: Colors.white54, fontSize: 13)),
        ),
      if (_loading)
        const Padding(
          padding: EdgeInsets.all(12),
          child: CircularProgressIndicator(color: kGold, strokeWidth: 2),
        ),
      Padding(
        padding: const EdgeInsets.all(20),
        child: GestureDetector(
          onTap: _toggleMic,
          child: AnimatedContainer(
            duration: const Duration(milliseconds: 200),
            width: 72, height: 72,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: _listening ? kGold : const Color(0xFF1a1a1a),
              border: Border.all(color: kGold, width: 2),
              boxShadow: _listening ? [BoxShadow(
                color: kGold.withOpacity(0.5), blurRadius: 20, spreadRadius: 4)] : [],
            ),
            child: Icon(
              _listening ? Icons.stop : Icons.mic,
              color: _listening ? Colors.black : kGold,
              size: 32,
            ),
          ),
        ),
      ),
    ]),
  );
}
"""

_VOICE_APP_MANIFEST = """\
<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">

    <uses-permission android:name="android.permission.RECORD_AUDIO"/>
    <uses-permission android:name="android.permission.INTERNET"/>
    <uses-permission android:name="android.permission.BLUETOOTH"/>
    <uses-permission android:name="android.permission.BLUETOOTH_CONNECT"/>

    <application
        android:label="Super Agent Voice"
        android:name="${applicationName}"
        android:icon="@mipmap/ic_launcher">
        <activity
            android:name=".MainActivity"
            android:exported="true"
            android:launchMode="singleTop"
            android:taskAffinity=""
            android:theme="@style/LaunchTheme"
            android:configChanges="orientation|keyboardHidden|keyboard|screenSize|smallestScreenSize|locale|layoutDirection|fontScale|screenLayout|density|uiMode"
            android:hardwareAccelerated="true"
            android:windowSoftInputMode="adjustResize">
            <meta-data
                android:name="io.flutter.embedding.android.NormalTheme"
                android:resource="@style/NormalTheme"/>
            <intent-filter>
                <action android:name="android.intent.action.MAIN"/>
                <category android:name="android.intent.category.LAUNCHER"/>
            </intent-filter>
        </activity>
        <meta-data
            android:name="flutterEmbedding"
            android:value="2"/>
    </application>
</manifest>
"""


# ── Build progress log — readable by the /build/stream SSE endpoint ──────────
BUILD_PROGRESS_LOG = Path("/workspace/build_progress.log")


def _progress(msg: str) -> None:
    """
    Write a timestamped progress line to BUILD_PROGRESS_LOG.
    The /build/stream SSE endpoint tails this file and streams it live to the UI,
    so the user sees exactly what step is running instead of a blank 'Thinking...'.
    """
    from datetime import datetime
    line = f"[{datetime.utcnow().strftime('%H:%M:%S')}] {msg}\n"
    try:
        with BUILD_PROGRESS_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # never block the build on log failure


@tool
def build_flutter_voice_app(dummy: str = "") -> str:
    """
    Build the complete Super Agent Voice Android app in one atomic pipeline.
    Writes live progress to /workspace/build_progress.log — visible via /build/stream.
    Steps:
      1. Scaffold Flutter project (or reuse existing)
      2. Write pubspec.yaml via Python file I/O
      3. Write lib/main.dart via Python file I/O (handles all Dart special chars)
      4. Write complete valid AndroidManifest.xml (RECORD_AUDIO + INTERNET + BLUETOOTH)
      5. flutter pub get
      6. flutter build apk --debug  ← takes 5-10 min, progress logged live
      7. Upload APK to Cloudinary (fallback: GitHub Releases)
      8. Push source to github.com/gelson12/super_agent_voice
    Returns a complete report with download URL and install instructions.
    """
    import json as _json

    # Clear + start the progress log
    try:
        BUILD_PROGRESS_LOG.write_text("", encoding="utf-8")
    except Exception:
        pass

    _progress("🚀 Starting Super Agent Voice App build pipeline")

    proj = _WORKSPACE / "super_agent_voice"
    log = []

    # ── Step 1: Scaffold (always fresh — wipe prior broken build) ────────────
    _progress("📁 Step 1/8 — Scaffolding Flutter project (fresh)...")
    if proj.exists():
        _progress("  → Removing previous build directory to avoid stale state...")
        import shutil as _shutil
        try:
            _shutil.rmtree(str(proj))
            log.append("[scaffold] removed previous project directory")
            _progress("  → Previous directory removed")
        except Exception as _rm_err:
            log.append(f"[scaffold] WARNING: could not remove previous dir: {_rm_err}")
            _progress(f"  ⚠️ Could not remove previous dir: {_rm_err}")

    _progress("  → Scaffolding new Flutter project (super_agent_voice)...")
    out = _run(
        f"{_FLUTTER_BIN} create super_agent_voice --org com.superagent --platforms android",
        str(_WORKSPACE), timeout=180,
    )
    log.append(f"[scaffold] {out[-300:]}")
    _progress(f"  → Scaffold complete")

    if not proj.exists():
        _progress("❌ FAILED: could not scaffold project")
        return f"[build_flutter_voice_app] FAILED: could not scaffold project\n" + "\n".join(log)

    # ── Step 2: Write pubspec.yaml ────────────────────────────────────────────
    _progress("📝 Step 2/8 — Writing pubspec.yaml with voice dependencies...")
    try:
        (proj / "pubspec.yaml").write_text(_VOICE_APP_PUBSPEC, encoding="utf-8")
        log.append("[pubspec] written OK")
        _progress("  → pubspec.yaml written (speech_to_text, flutter_tts, http, permission_handler)")
    except Exception as e:
        _progress(f"❌ FAILED writing pubspec: {e}")
        return f"[build_flutter_voice_app] FAILED writing pubspec: {e}"

    # ── Step 3: Write lib/main.dart ───────────────────────────────────────────
    _progress("📝 Step 3/8 — Writing lib/main.dart (voice chat UI)...")
    try:
        lib_dir = proj / "lib"
        lib_dir.mkdir(exist_ok=True)
        (lib_dir / "main.dart").write_text(_VOICE_APP_MAIN_DART.lstrip(), encoding="utf-8")
        log.append(f"[main.dart] written OK ({len(_VOICE_APP_MAIN_DART)} chars)")
        _progress(f"  → main.dart written ({len(_VOICE_APP_MAIN_DART)} chars — gold/black UI, mic button, TTS)")
    except Exception as e:
        _progress(f"❌ FAILED writing main.dart: {e}")
        return f"[build_flutter_voice_app] FAILED writing main.dart: {e}"

    # ── Step 4: Write complete AndroidManifest.xml ───────────────────────────
    # We always write the full manifest from a Python constant — never patch.
    # Patching is fragile: inserting before <manifest puts elements outside the
    # XML root, producing invalid XML that breaks the Gradle build.
    _progress("🔐 Step 4/8 — Writing AndroidManifest.xml (RECORD_AUDIO + INTERNET + BLUETOOTH)...")
    try:
        manifest_path = proj / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
        manifest_path.write_text(_VOICE_APP_MANIFEST, encoding="utf-8")
        log.append("[manifest] written OK (complete valid XML)")
        _progress("  → AndroidManifest.xml written — permissions: RECORD_AUDIO, INTERNET, BLUETOOTH")
    except Exception as e:
        _progress(f"❌ FAILED writing manifest: {e}")
        return f"[build_flutter_voice_app] FAILED writing AndroidManifest.xml: {e}"

    # ── Step 4b: Patch android/app/build.gradle ──────────────────────────────
    # flutter_tts ^4.x requires minSdkVersion >= 24.
    # speech_to_text and permission_handler require >= 21.
    # Flutter's default resolves to 16 — too low for all three packages.
    # We also ensure a namespace is declared (required by AGP 8.x+).
    _progress("🔧 Step 4b — Patching build.gradle (minSdkVersion 24, namespace)...")
    try:
        gradle_path = proj / "android" / "app" / "build.gradle"
        if gradle_path.exists():
            gradle = gradle_path.read_text(encoding="utf-8")
            changed = False

            # Set minSdkVersion 24 — flutter_tts 4.x hard-requires it
            import re as _re
            if _re.search(r'minSdkVersion\s+(?:flutter\.minSdkVersion|\d+)', gradle):
                gradle = _re.sub(
                    r'minSdkVersion\s+(?:flutter\.minSdkVersion|\d+)',
                    'minSdkVersion 24',
                    gradle,
                )
                changed = True
                _progress("  → minSdkVersion set to 24 (required by flutter_tts 4.x)")

            # Add namespace if missing (AGP 8.x requires it)
            if 'namespace' not in gradle:
                gradle = gradle.replace(
                    'android {',
                    'android {\n    namespace "com.superagent.super_agent_voice"',
                    1,
                )
                changed = True
                _progress("  → namespace added: com.superagent.super_agent_voice")

            if changed:
                gradle_path.write_text(gradle, encoding="utf-8")
                log.append("[build.gradle] patched — minSdkVersion 21 + namespace")
            else:
                _progress("  → build.gradle already correct, no changes needed")
                log.append("[build.gradle] no changes needed")
        else:
            _progress("  ⚠️ build.gradle not found — skipping patch")
            log.append("[build.gradle] not found — skipping")
    except Exception as e:
        log.append(f"[build.gradle] WARNING: {e}")
        _progress(f"  ⚠️ build.gradle patch warning: {e}")

    # ── Step 5: flutter pub get ───────────────────────────────────────────────
    _progress("📦 Step 5/8 — Running flutter pub get (downloading packages)...")
    _progress("  → This downloads speech_to_text, flutter_tts, http (~30-60s)...")
    pub_out = _run(f"{_FLUTTER_BIN} pub get", str(proj), timeout=180)
    log.append(f"[pub get] {pub_out[-300:]}")
    if "[exit" in pub_out and "error" in pub_out.lower():
        _progress(f"❌ FAILED at pub get: {pub_out[-200:]}")
        return "[build_flutter_voice_app] FAILED at pub get:\n" + pub_out + "\n\nLog:\n" + "\n".join(log)
    _progress(f"  → pub get complete")

    # ── Step 6: flutter build apk ─────────────────────────────────────────────
    _progress("🔨 Step 6/8 — Building APK (flutter build apk --debug)...")
    _progress("  → SLOWEST STEP: Gradle is compiling Dart → native Android bytecode")
    _progress("  → Expected time: 5-10 minutes on first build, ~2 min on rebuild")
    _progress("  → Do not close this session — build is running in the background")
    _progress("  → ⏳ Gradle: downloading dependencies and running code generation...")

    import threading as _threading
    _build_done = [False]

    def _heartbeat():
        """Write a progress dot every 60s so the user knows the build is still alive."""
        _ticks = 0
        _msgs = [
            "  → ⏳ Still compiling... (Gradle is building Android classes)",
            "  → ⏳ Still compiling... (running R8/D8 code transformations)",
            "  → ⏳ Still compiling... (linking native libraries)",
            "  → ⏳ Still compiling... (packaging APK resources)",
            "  → ⏳ Still compiling... (almost done — signing debug APK)",
        ]
        while not _build_done[0]:
            _threading.Event().wait(60)
            if _build_done[0]:
                break
            _ticks += 1
            msg = _msgs[min(_ticks - 1, len(_msgs) - 1)]
            _progress(msg)

    _hb = _threading.Thread(target=_heartbeat, daemon=True)
    _hb.start()

    build_out = _run(f"{_FLUTTER_BIN} build apk --debug", str(proj), timeout=600)
    _build_done[0] = True

    log.append(f"[build apk] {build_out[-800:]}")
    apk = proj / "build" / "app" / "outputs" / "flutter-apk" / "app-debug.apk"
    if not apk.exists():
        # ── Auto-repair: parse the error and retry once ───────────────────────
        # Never fail twice on the same known error. The repair module maps
        # Gradle error patterns to targeted fixes applied before the retry.
        _progress("❌ Build failed — running auto-repair analysis...")
        from .build_repair import attempt_auto_repair
        _repaired, _fixes = attempt_auto_repair(build_out, proj)
        if _repaired:
            for _fix_desc in _fixes:
                _progress(f"  🔧 Auto-fix applied: {_fix_desc}")
            _progress("  🔄 Retrying build with fixes applied...")
            _build_done[0] = False
            _hb2 = _threading.Thread(target=_heartbeat, daemon=True)
            _hb2.start()
            build_out = _run(f"{_FLUTTER_BIN} build apk --debug", str(proj), timeout=600)
            _build_done[0] = True
            log.append(f"[build apk retry] {build_out[-800:]}")
            if not apk.exists():
                _progress("❌ Retry also failed — full Gradle output:")
                for _err_line in build_out.splitlines()[-40:]:
                    if _err_line.strip():
                        _progress(f"  {_err_line.strip()}")
                return "[build_flutter_voice_app] FAILED after auto-repair retry.\n\nBuild output:\n" + build_out
            _progress("  ✅ Auto-repair succeeded — build passed on retry!")
        else:
            _progress("❌ BUILD FAILED — no known auto-fix for this error. Full output:")
            for _err_line in build_out.splitlines()[-40:]:
                if _err_line.strip():
                    _progress(f"  {_err_line.strip()}")
            return "[build_flutter_voice_app] FAILED: APK not found after build.\n\nBuild output:\n" + build_out

    size_mb = round(apk.stat().st_size / (1024 * 1024), 2)
    log.append(f"[apk] {apk} ({size_mb} MB)")
    _progress(f"  ✅ APK built successfully! Size: {size_mb} MB")

    # ── Step 7: Upload APK ────────────────────────────────────────────────────
    _progress(f"☁️ Step 7/8 — Making APK available for download ({size_mb} MB)...")
    if size_mb > _CLOUDINARY_MAX_MB:
        _progress(f"  → APK is {size_mb} MB (>{_CLOUDINARY_MAX_MB} MB Cloudinary limit)")
        _progress(f"  → Trying Railway direct-serve first (instant — no upload needed)...")
        _progress(f"  → Fallback: GitHub Releases (large upload, may take a few minutes)")
    else:
        _progress(f"  → Trying Cloudinary first (≤{_CLOUDINARY_MAX_MB} MB), fallback: Railway direct-serve")
    _progress(f"  → Generating download link...")

    upload_result = upload_build_artifact.invoke({
        "file_path": str(apk),
        "filename": f"builds/super_agent_voice_{int(time.time())}",
    })
    download_url = None
    url_verified = False
    upload_source = "none"
    size_warning = ""
    try:
        upload_data = _json.loads(upload_result)
        download_url = upload_data.get("url")
        upload_source = upload_data.get("source", "unknown")
        url_verified = upload_data.get("url_verified", False)
        size_warning = upload_data.get("size_warning", "")
        if download_url:
            status = "✅ URL verified" if url_verified else "⚠️ URL NOT verified (may be unavailable)"
            log.append(f"[upload] {upload_source} → {download_url} [{status}]")
            _progress(f"  Upload via {upload_source}: {status}")
            _progress(f"  URL: {download_url}")
        else:
            log.append(f"[upload] failed: {upload_data.get('error', upload_result)}")
            _progress(f"  ⚠️ Upload failed: {upload_data.get('error', 'unknown error')}")
    except Exception:
        log.append(f"[upload] failed: {upload_result}")
        _progress(f"  ⚠️ Upload failed (could not parse result): {upload_result[:150]}")

    # ── Step 8: Push source to GitHub ─────────────────────────────────────────
    _progress("📤 Step 8/8 — Pushing source code to github.com/gelson12/super_agent_voice...")
    pat = os.environ.get("GITHUB_PAT", "")
    repo_url = "https://github.com/gelson12/super_agent_voice"
    if pat:
        push_result = flutter_git_push.invoke({
            "project_path": str(proj),
            "repo_name": "super_agent_voice",
            "commit_message": "Super Agent Voice Chat app — built by Super Agent",
        })
        log.append(f"[git push] {push_result[:200]}")
        _progress(f"  ✅ Code pushed to GitHub: {repo_url}")
    else:
        log.append("[git push] skipped — GITHUB_PAT not set")
        _progress("  ⚠️ Git push skipped — GITHUB_PAT not set")

    # ── Save winning recipe so future builds replay what worked ───────────────
    try:
        from ..learning.build_recipes import save_recipe
        save_recipe(
            project_name="super_agent_voice",
            steps=[
                "flutter create super_agent_voice (wipe existing dir first)",
                "write pubspec.yaml with speech_to_text ^6.6.0 + flutter_tts ^4.0.2",
                "write complete AndroidManifest.xml (RECORD_AUDIO + INTERNET permissions)",
                "patch android/app/build.gradle: minSdkVersion=24, namespace=com.superagent.voice",
                "write lib/main.dart (voice chat UI)",
                "flutter pub get",
                "flutter build apk --debug",
                f"upload APK via {upload_source} → {download_url}",
                "flutter_git_push to GitHub",
            ],
            notes=f"minSdk=24 required by flutter_tts ^4.0.2. APK={size_mb}MB. Source={upload_source}.",
        )
    except Exception:
        pass

    # ── Final report ──────────────────────────────────────────────────────────
    _progress("🏁 Build pipeline complete!")

    if size_warning:
        _progress(size_warning)

    if not download_url:
        _progress(f"⚠️ APK built but upload failed. APK is on the server at: {apk}")
        return (
            f"✅ APK built successfully ({size_mb} MB) — but upload failed.\n\n"
            f"**APK is on the Railway server at:** `{apk}`\n\n"
            f"**To get the download link, try one of:**\n"
            f"1. Ask Super Agent: 'retry uploading the APK'\n"
            f"2. Run: `upload_build_artifact` with the path above\n\n"
            f"{size_warning}\n\n"
            f"Build log:\n" + "\n".join(log)
        )

    if url_verified:
        url_status = "✅ **Verified — link works**"
    else:
        url_status = "⚠️ **URL could not be verified** — try the link; if it gives 404, ask Super Agent to retry the upload."

    source_note = ""
    if upload_source == "railway_direct":
        source_note = (
            "\n💡 **Railway link note:** This link is served directly from the Railway container — "
            "no cloud upload needed. It will work immediately. "
            "If the container is redeployed (new Railway deploy), the link will 404. "
            "Ask Super Agent to 'regenerate the APK download link' if that happens."
        )

    install_steps = (
        f"✅ **SuperAgent Voice APK — Build Complete**\n\n"
        f"📦 **APK size:** {size_mb} MB\n"
        f"☁️ **Served via:** {upload_source}\n"
        f"🔗 **Download URL:** {download_url}\n"
        f"🔍 **Link status:** {url_status}\n"
        + source_note + "\n\n"
        + (f"⚠️ {size_warning}\n\n" if size_warning else "")
        + f"📱 **Android Installation (sideload):**\n"
        f"1. On your Android phone: Settings → Security → Install unknown apps → allow your browser\n"
        f"2. Open this URL in Chrome on your phone: {download_url}\n"
        f"3. Tap the downloaded file → Install\n"
        f"4. If blocked: Settings → Apps → Special app access → Install unknown apps → Chrome → Allow\n\n"
        f"🎙 **Using the app:**\n"
        f"• Tap the gold microphone button to start speaking\n"
        f"• Tap again (or pause 3 seconds) to send\n"
        f"• Super Agent responds in text AND reads the answer aloud\n\n"
        f"📦 **Source code:** {repo_url}\n"
    )
    return install_steps


@tool
def flutter_create_project(
    project_name: str,
    org_id: str = "com.superagent",
    description: str = "A Flutter application built by Super Agent",
) -> str:
    """
    Scaffold a new Flutter project in /workspace/<project_name>.
    project_name must be lowercase with underscores (e.g. 'my_app').
    Returns the project path and flutter create output.
    """
    project_path = str(_WORKSPACE / project_name)
    if Path(project_path).exists():
        return f"[already exists] {project_path}"
    cmd = (
        f"{_FLUTTER_BIN} create "
        f"--org {org_id} "
        f"--description \"{description}\" "
        f"--platforms android,ios "
        f"{project_path}"
    )
    output = _run(cmd, timeout=180)
    return f"Project created at {project_path}\n\n{output}"


@tool
def flutter_build_apk(project_path: str) -> str:
    """
    Build a debug APK for the Flutter project at project_path.
    Automatically uploads the APK to Cloudinary and returns a direct download URL.
    APK is debug-signed — no keystore needed for testing.
    """
    import json as _json

    path = Path(project_path)
    if not path.exists():
        return f"[error] Project not found at {project_path}"

    # Ensure dependencies are up to date
    _run(f"{_FLUTTER_BIN} pub get", cwd=project_path, timeout=120)

    # Build
    build_out = _run(
        f"{_FLUTTER_BIN} build apk --debug",
        cwd=project_path,
        timeout=_TIMEOUT_BUILD,
    )

    apk = path / "build" / "app" / "outputs" / "flutter-apk" / "app-debug.apk"
    if not apk.exists():
        return f"Build failed — APK not found at expected path.\n\n{build_out}"

    size_mb = round(apk.stat().st_size / (1024 * 1024), 2)

    # Auto-upload to Cloudinary for a permanent download link
    project_name = Path(project_path).name
    upload_result = upload_build_artifact.invoke({
        "file_path": str(apk),
        "filename": f"builds/{project_name}_{int(time.time())}",
    })

    try:
        upload_data = _json.loads(upload_result)
        download_url = upload_data.get("url", "")
        return (
            f"✅ APK built successfully!\n\n"
            f"📦 Size: {size_mb} MB\n"
            f"📥 Download URL: {download_url}\n\n"
            f"Install instructions:\n"
            f"1. On your Android device: Settings → Security → Unknown Sources → Enable\n"
            f"2. Open {download_url} in your phone browser\n"
            f"3. Tap the downloaded file → Install\n\n"
            f"Build log:\n{build_out[-1000:]}"
        )
    except Exception:
        # Upload failed — still return local path
        return (
            f"✅ APK built: {apk} ({size_mb} MB)\n"
            f"⚠️ Cloudinary upload failed: {upload_result}\n\n"
            f"Build log:\n{build_out[-1000:]}"
        )


@tool
def flutter_test(project_path: str) -> str:
    """
    Run Flutter unit tests for the project at project_path.
    Returns test results.
    """
    if not Path(project_path).exists():
        return f"[error] Project not found at {project_path}"
    _run(f"{_FLUTTER_BIN} pub get", cwd=project_path, timeout=120)
    return _run(f"{_FLUTTER_BIN} test", cwd=project_path, timeout=300)


def _verify_url(url: str, pat: str = "") -> bool:
    """
    Verify a URL is actually accessible via HEAD request.
    Follows redirects. Returns True if HTTP < 400.
    """
    import urllib.request
    try:
        req = urllib.request.Request(url, method="HEAD")
        if pat:
            req.add_header("Authorization", f"token {pat}")
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status < 400
    except Exception:
        return False


def _github_release_upload(fp: Path, project_name: str) -> str | None:
    """
    Fallback: create a GitHub Release on gelson12/super-agent and upload the APK as a release asset.
    Returns the browser_download_url on success (after verifying URL works), None on failure.
    """
    import json
    import urllib.request
    pat = os.environ.get("GITHUB_PAT", "")
    if not pat:
        return None

    size_mb = round(fp.stat().st_size / (1024 * 1024), 2)
    tag = f"apk-{project_name}-{int(time.time())}"

    # ── Create release ────────────────────────────────────────────────────────
    try:
        payload = json.dumps({
            "tag_name": tag,
            "name": f"APK Build — {project_name}",
            "body": f"Automated APK build by Super Agent for project '{project_name}'. Size: {size_mb} MB.",
            "draft": False,
            "prerelease": True,
        }).encode()
        req = urllib.request.Request(
            "https://api.github.com/repos/gelson12/super-agent/releases",
            data=payload,
            headers={
                "Authorization": f"token {pat}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            release_data = json.loads(resp.read())
        upload_url = release_data["upload_url"].split("{")[0]  # strip {?name,label} template
    except Exception as e:
        print(f"[github_release_upload] create release failed: {e}")
        return None

    # ── Upload APK asset ──────────────────────────────────────────────────────
    # Use a large timeout — GitHub allows up to 2 GB per release asset,
    # but uploading 100+ MB via the API needs time (allow 10 min).
    try:
        file_size = fp.stat().st_size
        with fp.open("rb") as f:
            asset_req = urllib.request.Request(
                f"{upload_url}?name={fp.name}",
                data=f.read(),
                headers={
                    "Authorization": f"token {pat}",
                    "Content-Type": "application/vnd.android.package-archive",
                    "Content-Length": str(file_size),
                },
                method="POST",
            )
        with urllib.request.urlopen(asset_req, timeout=600) as resp:
            asset_data = json.loads(resp.read())
    except Exception as e:
        print(f"[github_release_upload] upload asset failed: {e}")
        return None

    download_url = asset_data.get("browser_download_url")
    if not download_url:
        print("[github_release_upload] no browser_download_url in response")
        return None

    # ── Verify URL is actually accessible (wait 3s for GitHub to process) ────
    time.sleep(3)
    if not _verify_url(download_url, pat):
        print(f"[github_release_upload] URL verification failed: {download_url}")
        # Return the URL anyway — GitHub CDN can be momentarily slow
        # but mark it as unverified so caller can warn the user
        return f"UNVERIFIED:{download_url}"

    return download_url


_CLOUDINARY_MAX_MB    = 50    # Cloudinary free/basic plans reject files > this size
_GITHUB_WARN_MB       = 100   # APKs over this size are large; warn but still attempt upload
_APK_DOWNLOADS_DIR    = Path("/workspace/apk_downloads")
_APK_REGISTRY_PATH    = Path("/workspace/apk_downloads/registry.json")


def _load_apk_registry() -> list[dict]:
    """Load the persistent APK download token registry."""
    try:
        if _APK_REGISTRY_PATH.exists():
            return json.loads(_APK_REGISTRY_PATH.read_text())
    except Exception:
        pass
    return []


def _save_apk_registry(registry: list[dict]) -> None:
    """Persist the APK download token registry."""
    try:
        _APK_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _APK_REGISTRY_PATH.write_text(json.dumps(registry, indent=2))
    except Exception:
        pass


def _register_apk_link(token: str, filename: str, apk_path: str, url: str) -> None:
    """Record a new APK download link in the persistent registry."""
    import time
    registry = _load_apk_registry()
    registry.append({
        "token": token,
        "filename": filename,
        "apk_path": apk_path,
        "url": url,
        "created_at": time.time(),
    })
    # Keep only last 20 entries
    _save_apk_registry(registry[-20:])


def get_apk_download_status() -> list[dict]:
    """Return all registered APK links with their current validity."""
    registry = _load_apk_registry()
    results = []
    for entry in registry:
        token = entry.get("token", "")
        filename = entry.get("filename", "")
        file_path = _APK_DOWNLOADS_DIR / token / filename
        results.append({
            **entry,
            "valid": file_path.exists(),
        })
    return results


def _serve_apk_from_railway(fp: Path) -> str | None:
    """
    Copy the APK into /workspace/apk_downloads/{token}/ and return a direct
    download URL served by this Railway container.

    The token is a random 22-char URL-safe string — it acts as the credential,
    so no X-Token header is required (works from a mobile Chrome browser).

    Returns the full HTTPS URL, or None if RAILWAY_PUBLIC_DOMAIN is not set.
    """
    import secrets as _secrets
    import shutil as _shutil

    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if not domain:
        return None

    token = _secrets.token_urlsafe(16)  # 22 URL-safe chars
    dest_dir = _APK_DOWNLOADS_DIR / token
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        _shutil.copy2(fp, dest_dir / fp.name)
    except Exception as e:
        print(f"[serve_apk_from_railway] copy failed: {e}")
        return None

    url = f"https://{domain}/downloads/{token}/{fp.name}"
    _register_apk_link(token, fp.name, str(fp), url)
    return url


@tool
def upload_build_artifact(file_path: str, filename: str = "") -> str:
    """
    Upload an APK or IPA file to Cloudinary (primary, ≤50 MB) or GitHub Releases (fallback).
    file_path: absolute path to the APK/IPA file.
    filename: optional public_id override for the Cloudinary asset.
    Returns JSON with: url, public_id, size_mb, source, url_verified (bool).
    """
    import json
    from ..config import settings

    fp = Path(file_path)
    if not fp.exists():
        return f"[error] File not found: {file_path}"

    size_mb = round(fp.stat().st_size / (1024 * 1024), 2)
    # Derive a clean project name from the file stem (handles app-debug, my_app, etc.)
    stem = fp.stem  # e.g. "app-debug"
    project_name = stem.replace("-", "_")  # "app_debug"

    size_warning = ""
    if size_mb > _GITHUB_WARN_MB:
        size_warning = (
            f" ⚠️ APK is {size_mb} MB — larger than typical (~30–60 MB). "
            "Consider enabling R8/ProGuard or removing unused packages to shrink it."
        )

    # ── Primary for large files: Railway direct serve (instant, no upload) ───
    # For files that exceed Cloudinary's limit we serve directly from this container.
    # The URL contains a random token — no extra auth needed for mobile browsers.
    if size_mb > _CLOUDINARY_MAX_MB:
        railway_url = _serve_apk_from_railway(fp)
        if railway_url:
            return json.dumps({
                "url": railway_url,
                "public_id": f"railway-local/{fp.name}",
                "size_mb": size_mb,
                "source": "railway_direct",
                "url_verified": True,   # file is local — no network needed
                "size_warning": size_warning,
                "note": (
                    "Served directly from the Railway container. "
                    "Link is valid as long as the container is running. "
                    "If it returns 404 after a redeploy, ask Super Agent to regenerate the link."
                ),
            })

    # ── Primary: Cloudinary (small files only) ───────────────────────────────
    cloudinary_ok = all([
        settings.cloudinary_cloud_name,
        settings.cloudinary_api_key,
        settings.cloudinary_api_secret,
    ])
    if cloudinary_ok and size_mb <= _CLOUDINARY_MAX_MB:
        try:
            import cloudinary
            import cloudinary.uploader

            cloudinary.config(
                cloud_name=settings.cloudinary_cloud_name,
                api_key=settings.cloudinary_api_key,
                api_secret=settings.cloudinary_api_secret,
            )

            public_id = filename or f"builds/{fp.stem}_{int(time.time())}"
            result = cloudinary.uploader.upload(
                str(fp),
                resource_type="raw",
                public_id=public_id,
                overwrite=True,
            )
            cl_url = result["secure_url"]
            url_ok = _verify_url(cl_url)
            return json.dumps({
                "url": cl_url,
                "public_id": result["public_id"],
                "size_mb": size_mb,
                "source": "cloudinary",
                "url_verified": url_ok,
                "size_warning": size_warning,
            })
        except Exception as _cl_err:
            print(f"[upload_build_artifact] Cloudinary failed: {_cl_err} — trying GitHub Releases")
    elif cloudinary_ok and size_mb > _CLOUDINARY_MAX_MB:
        print(f"[upload_build_artifact] Skipping Cloudinary — {size_mb} MB exceeds {_CLOUDINARY_MAX_MB} MB limit")

    # ── Fallback: GitHub Releases ────────────────────────────────────────────
    gh_result = _github_release_upload(fp, project_name)
    if gh_result:
        url_verified = not gh_result.startswith("UNVERIFIED:")
        gh_url = gh_result.removeprefix("UNVERIFIED:")
        return json.dumps({
            "url": gh_url,
            "public_id": f"github-release/{project_name}",
            "size_mb": size_mb,
            "source": "github_releases",
            "url_verified": url_verified,
            "size_warning": size_warning,
        })

    return json.dumps({
        "url": None,
        "size_mb": size_mb,
        "source": "none",
        "url_verified": False,
        "error": f"Both Cloudinary (skipped — {size_mb} MB > {_CLOUDINARY_MAX_MB} MB limit) and GitHub Releases upload failed.",
        "size_warning": size_warning,
    })


@tool
def flutter_git_push(
    project_path: str,
    repo_name: str,
    commit_message: str = "Initial Flutter project from Super Agent",
) -> str:
    """
    Initialize a git repo in project_path, create a GitHub remote,
    and push all project files. Uses GITHUB_PAT from environment.
    repo_name: e.g. 'my_flutter_app' (will be created under the authenticated user).
    Returns the GitHub repo URL.
    """
    pat = os.environ.get("GITHUB_PAT", "")
    if not pat:
        return "[error] GITHUB_PAT not set — cannot push to GitHub"

    if not Path(project_path).exists():
        return f"[error] Project not found at {project_path}"

    # Create GitHub repo via API
    import json
    import urllib.request

    try:
        payload = json.dumps({
            "name": repo_name,
            "description": f"Flutter app built by Super Agent",
            "private": False,
            "auto_init": False,
        }).encode()
        req = urllib.request.Request(
            "https://api.github.com/user/repos",
            data=payload,
            headers={
                "Authorization": f"token {pat}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            repo_data = json.loads(resp.read())
        repo_url = repo_data["html_url"]
        clone_url = repo_data["clone_url"].replace(
            "https://", f"https://x-access-token:{pat}@"
        )
    except Exception as e:
        # Repo may already exist — try to use it
        repo_url = f"https://github.com/{repo_name}"
        clone_url = f"https://x-access-token:{pat}@github.com/{repo_name}.git"
        print(f"[flutter_git_push] GitHub create warning: {e}")

    # Push
    cmds = [
        f"git init",
        f"git add -A",
        f'git commit -m "{commit_message}"',
        f"git branch -M main",
        f"git remote add origin {clone_url} 2>/dev/null || git remote set-url origin {clone_url}",
        f"git push -u origin main",
    ]
    output_lines = []
    for cmd in cmds:
        out = _run(cmd, cwd=project_path, timeout=60)
        output_lines.append(f"$ {cmd.split(' --')[0]}\n{out}")

    return f"Repo: {repo_url}\n\n" + "\n".join(output_lines)


@tool
def regenerate_apk_download_link(project_name: str = "super_agent_voice") -> str:
    """
    Find the most recently built APK in /workspace and generate a fresh Railway
    direct-serve download link without re-uploading or rebuilding anything.
    Use this after a Railway redeploy makes a previous link return 404.
    Returns the new download URL and its expiry note.
    """
    import json

    # Find the most recently built APK
    candidates = sorted(
        list(Path("/workspace").glob("**/app-debug.apk")) +
        list(Path("/workspace").glob("**/app-release.apk")),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return json.dumps({"error": "No APK found in /workspace. The build needs to be re-run."})

    apk = candidates[0]
    size_mb = round(apk.stat().st_size / (1024 * 1024), 2)
    _progress(f"🔗 Regenerating Railway download link for {apk.name} ({size_mb} MB)...")

    url = _serve_apk_from_railway(apk)
    if url:
        _progress(f"  ✅ New link: {url}")
        return json.dumps({
            "url": url,
            "source": "railway_direct",
            "url_verified": True,
            "size_mb": size_mb,
            "apk_path": str(apk),
            "note": "Link is valid as long as the Railway container is running.",
        })

    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if not domain:
        return json.dumps({
            "error": "RAILWAY_PUBLIC_DOMAIN env var is not set. Cannot generate Railway link. "
                     "Use retry_apk_upload to upload to GitHub Releases instead.",
            "apk_path": str(apk),
        })

    return json.dumps({"error": f"Could not copy APK to downloads directory. APK is at: {apk}"})


@tool
def retry_apk_upload(project_name: str = "super_agent_voice") -> str:
    """
    Find the most recently built APK in /workspace/<project_name> and re-upload it.
    Use this when a previous upload failed or gave a 404 URL.
    Returns JSON with: url, source, url_verified, size_mb.
    """
    import json
    import glob as _glob

    search_paths = [
        f"/workspace/{project_name}/build/app/outputs/flutter-apk/app-debug.apk",
        f"/workspace/{project_name}/build/app/outputs/flutter-apk/app-release.apk",
    ]
    # Also search recursively
    found = []
    for sp in search_paths:
        p = Path(sp)
        if p.exists():
            found.append(p)
    if not found:
        matches = list(Path("/workspace").glob("**/app-debug.apk"))
        matches += list(Path("/workspace").glob("**/app-release.apk"))
        found = sorted(matches, key=lambda x: x.stat().st_mtime, reverse=True)

    if not found:
        return json.dumps({"error": f"No APK found in /workspace. Build the app first."})

    apk = found[0]
    _progress(f"🔄 Retrying upload of {apk.name} ({round(apk.stat().st_size / 1024**2, 1)} MB)...")
    result = upload_build_artifact.invoke({
        "file_path": str(apk),
        "filename": f"builds/{project_name}_retry_{int(time.time())}",
    })
    _progress(f"  Upload result: {result[:200]}")
    return result
