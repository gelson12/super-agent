"""
Build repair module — autonomous Gradle/Flutter error diagnosis and fix.

When a Flutter build fails, call `attempt_auto_repair(build_output, proj_path)`
before giving up. It parses the Gradle error, applies the known fix in-place,
and returns True if a fix was applied (caller should retry the build).

This is the "never fail twice on the same error" system — every known error
gets a fix rule, and that fix is applied automatically on the next build attempt.
"""
import re
from pathlib import Path


# ── Known error patterns → fix functions ─────────────────────────────────────
# Each entry: (regex pattern to match in build output, fix function name)
# Fix functions receive (proj: Path, build_output: str) and return a description
# of what was fixed, or None if the pattern matched but fix couldn't be applied.

def _fix_minsdk(proj: Path, build_output: str) -> str | None:
    """Extract the required minSdk from the error and patch build.gradle."""
    # Error: "increase this project's minSdk version to at least 24"
    match = re.search(r'minSdk(?:Version)?\s+(?:version\s+)?to at least\s+(\d+)', build_output, re.IGNORECASE)
    required = int(match.group(1)) if match else 24
    gradle_path = proj / "android" / "app" / "build.gradle"
    if not gradle_path.exists():
        return None
    gradle = gradle_path.read_text(encoding="utf-8")
    new_gradle = re.sub(
        r'minSdkVersion\s+\d+',
        f'minSdkVersion {required}',
        gradle,
    )
    if new_gradle == gradle:
        # Pattern not found — insert it into defaultConfig block
        new_gradle = re.sub(
            r'(defaultConfig\s*\{)',
            f'\\1\n        minSdkVersion {required}',
            gradle,
        )
    if new_gradle != gradle:
        gradle_path.write_text(new_gradle, encoding="utf-8")
        return f"Set minSdkVersion to {required} in build.gradle"
    return None


def _fix_namespace(proj: Path, build_output: str) -> str | None:
    """Add missing namespace declaration to build.gradle."""
    gradle_path = proj / "android" / "app" / "build.gradle"
    if not gradle_path.exists():
        return None
    gradle = gradle_path.read_text(encoding="utf-8")
    if 'namespace' in gradle:
        return None  # already there
    new_gradle = gradle.replace(
        'android {',
        'android {\n    namespace "com.superagent.super_agent_voice"',
        1,
    )
    if new_gradle != gradle:
        gradle_path.write_text(new_gradle, encoding="utf-8")
        return "Added namespace to build.gradle"
    return None


def _fix_flutter_clean(proj: Path, build_output: str) -> str | None:
    """Run flutter clean to clear corrupted build cache."""
    import subprocess, os
    flutter_bin = os.environ.get("FLUTTER_HOME", "/opt/flutter") + "/bin/flutter"
    try:
        result = subprocess.run(
            f"{flutter_bin} clean",
            shell=True, cwd=str(proj), capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            return "Ran flutter clean to clear corrupted build cache"
    except Exception:
        pass
    return None


def _fix_compilesdk(proj: Path, build_output: str) -> str | None:
    """Bump compileSdkVersion to 34 if too low."""
    gradle_path = proj / "android" / "app" / "build.gradle"
    if not gradle_path.exists():
        return None
    gradle = gradle_path.read_text(encoding="utf-8")
    new_gradle = re.sub(r'compileSdk(?:Version)?\s+\d+', 'compileSdkVersion 34', gradle)
    if new_gradle != gradle:
        gradle_path.write_text(new_gradle, encoding="utf-8")
        return "Bumped compileSdkVersion to 34"
    return None


def _fix_java_compatibility(proj: Path, build_output: str) -> str | None:
    """Add Java 8 source/target compatibility to build.gradle."""
    gradle_path = proj / "android" / "app" / "build.gradle"
    if not gradle_path.exists():
        return None
    gradle = gradle_path.read_text(encoding="utf-8")
    if 'sourceCompatibility' in gradle:
        return None
    new_gradle = gradle.replace(
        'android {',
        'android {\n    compileOptions {\n        sourceCompatibility JavaVersion.VERSION_1_8\n        targetCompatibility JavaVersion.VERSION_1_8\n    }',
        1,
    )
    if new_gradle != gradle:
        gradle_path.write_text(new_gradle, encoding="utf-8")
        return "Added Java 8 compileOptions to build.gradle"
    return None


# ── Error pattern registry ────────────────────────────────────────────────────

_REPAIR_RULES: list[tuple[str, object]] = [
    # Pattern (case-insensitive substring)  →  fix function
    ("minsdk",                              _fix_minsdk),
    ("minsdkversion",                       _fix_minsdk),
    ("increase this project's minsdk",      _fix_minsdk),
    ("namespace",                           _fix_namespace),
    ("compilesdkversion",                   _fix_compilesdk),
    ("compilesdk",                          _fix_compilesdk),
    ("source compatibility",                _fix_java_compatibility),
    ("duplicate class",                     _fix_flutter_clean),
    ("could not resolve",                   _fix_flutter_clean),
    ("cached resource",                     _fix_flutter_clean),
    ("task ':app:compile",                  _fix_flutter_clean),
    ("build cache",                         _fix_flutter_clean),
]


def attempt_auto_repair(build_output: str, proj: Path) -> tuple[bool, list[str]]:
    """
    Analyse a failed build output, apply all matching fixes, return
    (any_fix_applied: bool, descriptions: list[str]).

    Caller should retry the build if any_fix_applied is True.
    """
    lower = build_output.lower()
    applied: list[str] = []
    seen_fns: set = set()

    for pattern, fix_fn in _REPAIR_RULES:
        if pattern in lower and fix_fn not in seen_fns:
            seen_fns.add(fix_fn)
            try:
                result = fix_fn(proj, build_output)
                if result:
                    applied.append(result)
            except Exception as e:
                applied.append(f"[repair warning] {fix_fn.__name__}: {e}")

    return bool(applied), applied
