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

_VOICE_APP_MANIFEST_PERMISSIONS = """\
    <uses-permission android:name="android.permission.RECORD_AUDIO"/>
    <uses-permission android:name="android.permission.INTERNET"/>
    <uses-permission android:name="android.permission.BLUETOOTH"/>
    <uses-permission android:name="android.permission.BLUETOOTH_CONNECT"/>
"""


@tool
def build_flutter_voice_app(dummy: str = "") -> str:
    """
    Build the complete Super Agent Voice Android app in one atomic pipeline.
    This tool handles EVERYTHING in Python — no heredocs, no LLM decisions mid-build:
      1. Scaffold Flutter project (or reuse existing)
      2. Write pubspec.yaml via Python file I/O
      3. Write lib/main.dart via Python file I/O (handles all Dart special chars)
      4. Patch AndroidManifest.xml with microphone + internet permissions
      5. flutter pub get
      6. flutter build apk --debug
      7. Upload APK to Cloudinary (fallback: GitHub Releases)
      8. Push source to github.com/gelson12/super_agent_voice
    Returns a complete report with download URL and install instructions.
    """
    import json as _json
    from ..config import settings as _settings

    proj = _WORKSPACE / "super_agent_voice"
    log = []

    # ── Step 1: Scaffold if not exists ───────────────────────────────────────
    if not proj.exists():
        out = _run(
            f"{_FLUTTER_BIN} create super_agent_voice --org com.superagent --platforms android",
            str(_WORKSPACE), timeout=180,
        )
        log.append(f"[scaffold] {out[-300:]}")
    else:
        log.append("[scaffold] reusing existing /workspace/super_agent_voice")

    if not proj.exists():
        return f"[build_flutter_voice_app] FAILED: could not scaffold project\n" + "\n".join(log)

    # ── Step 2: Write pubspec.yaml ────────────────────────────────────────────
    try:
        (proj / "pubspec.yaml").write_text(_VOICE_APP_PUBSPEC, encoding="utf-8")
        log.append("[pubspec] written OK")
    except Exception as e:
        return f"[build_flutter_voice_app] FAILED writing pubspec: {e}"

    # ── Step 3: Write lib/main.dart ───────────────────────────────────────────
    try:
        lib_dir = proj / "lib"
        lib_dir.mkdir(exist_ok=True)
        (lib_dir / "main.dart").write_text(_VOICE_APP_MAIN_DART.lstrip(), encoding="utf-8")
        log.append(f"[main.dart] written OK ({len(_VOICE_APP_MAIN_DART)} chars)")
    except Exception as e:
        return f"[build_flutter_voice_app] FAILED writing main.dart: {e}"

    # ── Step 4: Patch AndroidManifest.xml ────────────────────────────────────
    try:
        manifest_path = proj / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
        if manifest_path.exists():
            manifest = manifest_path.read_text(encoding="utf-8")
            if "RECORD_AUDIO" not in manifest:
                manifest = manifest.replace(
                    "<manifest",
                    _VOICE_APP_MANIFEST_PERMISSIONS + "<manifest",
                    1,
                )
                manifest_path.write_text(manifest, encoding="utf-8")
                log.append("[manifest] permissions patched")
            else:
                log.append("[manifest] permissions already present")
    except Exception as e:
        log.append(f"[manifest] WARNING: {e}")

    # ── Step 5: flutter pub get ───────────────────────────────────────────────
    pub_out = _run(f"{_FLUTTER_BIN} pub get", str(proj), timeout=180)
    log.append(f"[pub get] {pub_out[-300:]}")
    if "[exit" in pub_out and "error" in pub_out.lower():
        return "[build_flutter_voice_app] FAILED at pub get:\n" + pub_out + "\n\nLog:\n" + "\n".join(log)

    # ── Step 6: flutter build apk ─────────────────────────────────────────────
    build_out = _run(f"{_FLUTTER_BIN} build apk --debug", str(proj), timeout=600)
    log.append(f"[build apk] {build_out[-400:]}")
    apk = proj / "build" / "app" / "outputs" / "flutter-apk" / "app-debug.apk"
    if not apk.exists():
        return "[build_flutter_voice_app] FAILED: APK not found after build.\n\nBuild output:\n" + build_out

    size_mb = round(apk.stat().st_size / (1024 * 1024), 2)
    log.append(f"[apk] {apk} ({size_mb} MB)")

    # ── Step 7: Upload APK ────────────────────────────────────────────────────
    upload_result = upload_build_artifact.invoke({
        "file_path": str(apk),
        "filename": f"builds/super_agent_voice_{int(time.time())}",
    })
    try:
        upload_data = _json.loads(upload_result)
        download_url = upload_data["url"]
        upload_source = upload_data.get("source", "unknown")
        log.append(f"[upload] {upload_source} → {download_url}")
    except Exception:
        download_url = None
        log.append(f"[upload] failed: {upload_result}")

    # ── Step 8: Push source to GitHub ─────────────────────────────────────────
    pat = os.environ.get("GITHUB_PAT", "")
    repo_url = "https://github.com/gelson12/super_agent_voice"
    if pat:
        push_result = flutter_git_push.invoke({
            "project_path": str(proj),
            "repo_name": "super_agent_voice",
            "commit_message": "Super Agent Voice Chat app — built by Super Agent",
        })
        log.append(f"[git push] {push_result[:200]}")
    else:
        log.append("[git push] skipped — GITHUB_PAT not set")

    # ── Final report ──────────────────────────────────────────────────────────
    if not download_url:
        return (
            f"✅ APK built ({size_mb} MB) but upload failed.\n"
            f"APK is at: {apk}\n\n"
            f"Log:\n" + "\n".join(log)
        )

    install_steps = (
        f"📥 **Download URL:** {download_url}\n\n"
        f"📱 **Android Installation (sideload):**\n"
        f"1. On your Android phone: Settings → Security → Install unknown apps → allow your browser\n"
        f"2. Open this URL in Chrome on your phone: {download_url}\n"
        f"3. Tap the downloaded APK file → Install\n"
        f"4. If blocked: Settings → Apps → Special app access → Install unknown apps → Chrome → Allow\n\n"
        f"🎙 **Using the app:**\n"
        f"• Tap the gold microphone button to start speaking\n"
        f"• Tap again (or pause 3 seconds) to send\n"
        f"• Super Agent responds in text AND reads the answer aloud\n\n"
        f"⚙️ **Voice settings (in-app):** The app uses English (en-US) at 0.5x speed by default.\n"
        f"To change: edit the _init() method in main.dart — _tts.setLanguage() and _tts.setSpeechRate()\n\n"
        f"📦 **Source code:** {repo_url}\n"
        f"📦 **APK size:** {size_mb} MB\n"
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


def _github_release_upload(fp: Path, project_name: str) -> str | None:
    """
    Fallback: create a GitHub Release on gelson12/super-agent and upload the APK as a release asset.
    Returns the browser_download_url on success, None on failure.
    """
    import json
    import urllib.request
    pat = os.environ.get("GITHUB_PAT", "")
    if not pat:
        return None
    tag = f"apk-{project_name}-{int(time.time())}"
    headers = {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }
    # Create a release
    try:
        payload = json.dumps({
            "tag_name": tag,
            "name": f"APK Build — {project_name}",
            "body": f"Automated APK build by Super Agent for project '{project_name}'.",
            "draft": False,
            "prerelease": True,
        }).encode()
        req = urllib.request.Request(
            "https://api.github.com/repos/gelson12/super-agent/releases",
            data=payload,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            release_data = json.loads(resp.read())
        upload_url = release_data["upload_url"].split("{")[0]  # strip {?name,label} template
        release_id = release_data["id"]
    except Exception as e:
        print(f"[github_release_upload] create release failed: {e}")
        return None

    # Upload the APK as an asset
    try:
        with fp.open("rb") as f:
            apk_bytes = f.read()
        asset_req = urllib.request.Request(
            f"{upload_url}?name={fp.name}",
            data=apk_bytes,
            headers={
                "Authorization": f"token {pat}",
                "Content-Type": "application/vnd.android.package-archive",
            },
            method="POST",
        )
        with urllib.request.urlopen(asset_req, timeout=120) as resp:
            asset_data = json.loads(resp.read())
        return asset_data["browser_download_url"]
    except Exception as e:
        print(f"[github_release_upload] upload asset failed: {e}")
        return None


@tool
def upload_build_artifact(file_path: str, filename: str = "") -> str:
    """
    Upload an APK or IPA file to Cloudinary (primary) or GitHub Releases (fallback).
    file_path: absolute path to the APK/IPA file.
    filename: optional public_id override for the Cloudinary asset.
    Returns JSON with: url, public_id, size_mb, source (cloudinary|github_releases).
    """
    import json
    from ..config import settings

    fp = Path(file_path)
    if not fp.exists():
        return f"[error] File not found: {file_path}"

    size_mb = round(fp.stat().st_size / (1024 * 1024), 2)
    project_name = fp.stem.split("_")[0] if "_" in fp.stem else fp.stem

    # ── Primary: Cloudinary ──────────────────────────────────────────────────
    cloudinary_ok = all([
        settings.cloudinary_cloud_name,
        settings.cloudinary_api_key,
        settings.cloudinary_api_secret,
    ])
    if cloudinary_ok:
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
            return json.dumps({
                "url": result["secure_url"],
                "public_id": result["public_id"],
                "size_mb": size_mb,
                "source": "cloudinary",
            })
        except Exception as _cl_err:
            print(f"[upload_build_artifact] Cloudinary failed: {_cl_err} — trying GitHub Releases")

    # ── Fallback: GitHub Releases ────────────────────────────────────────────
    gh_url = _github_release_upload(fp, project_name)
    if gh_url:
        return json.dumps({
            "url": gh_url,
            "public_id": f"github-release/{project_name}",
            "size_mb": size_mb,
            "source": "github_releases",
        })

    return f"[upload error] Both Cloudinary and GitHub Releases upload failed for {file_path}"


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
