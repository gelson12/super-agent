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
