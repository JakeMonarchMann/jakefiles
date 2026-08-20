"""Generic Windows EXE builder -- auto-detects everything.

Lives at <project>/builder/build_exe.py. Convention-driven; no edits
needed when the program grows or shrinks. To use for a new app, copy
this entire `builder/` folder verbatim.

Detection (all relative to PROJECT_ROOT = parent of this file):

  App name      -> PROJECT_ROOT folder name
  Entry script  -> top-level .py/.pyw with `if __name__ == "__main__":`
                   If multiple, prefer one whose stem matches the
                   folder name (.pyw wins ties), else app.py/main.py,
                   else MULTI-ENTRY MODE: build all as separate EXEs.
  Own packages  -> sibling dirs of builder/ with __init__.py
  Shared Drivers-> if <repo>/Drivers/ exists, scan project for
                   `from Drivers.X` / `import Drivers.X` imports and
                   add --paths + --collect-submodules for each X used.
                   Also auto-bundles the user-mode driver DLLs each
                   sub-driver needs at runtime (Thorlabs TLPMX, MCC
                   cbw64, ...) when found on the build PC, so target
                   PCs don't need those vendor SDKs installed. And
                   --collect-all's any third-party Python package a
                   sub-driver needs (e.g. MCC's mcculw) when it's
                   importable on the build PC, so the EXE is fully
                   self-contained (missing ones are warned, not fatal).
  Version       -> regex `.title("...vX.Y.Z")` in entry, else any file
  Icon          -> PROJECT_ROOT/icon.ico if present
  Manifest      -> builder/app.manifest if present (SmartScreen + DPI)
  Description   -> first line of entry's docstring (skipping shim/
                   compat/launcher docstrings), else app name
  Python ver    -> if run.bat pins `py -3.X`, the builder relaunches
                   itself under that interpreter (PySpin etc. need it)

Output: builder/dist/<App Name> v<X.Y.Z>.exe (single-entry) or
        builder/dist/<App Name> - <entry stem> v<X.Y.Z>.exe (multi).

SmartScreen mitigations: full version-info metadata, --noupx,
--windowed, exclude heavy unused deps. Without a code-signing cert
SmartScreen may still warn on first download until the file ages --
click "More info" -> "Run anyway".
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent

COMPANY = ""
COPYRIGHT = ""

# Heavyweight modules sometimes pulled in transitively by hooks even
# when the app never imports them. Excluding keeps the EXE lean and
# reduces SmartScreen surface area.
DEFAULT_EXCLUDES = [
    "pytest", "IPython", "jedi", "parso", "sphinx",
    "tensorflow", "torch", "notebook", "ipykernel",
    "matplotlib.tests", "numpy.tests", "scipy.tests",
    "pandas.tests", "sklearn.tests", "sklearn.datasets.tests",
    "numpy", "pandas", "scipy", "matplotlib",
]

ENTRY_PATTERN = re.compile(
    r'^\s*if\s+__name__\s*==\s*[\'"]__main__[\'"]', re.MULTILINE
)
TITLE_PATTERN = re.compile(r'\.title\([^)]*v(\d+)\.(\d+)\.(\d+)')
PY_VER_PATTERN = re.compile(r'\bpy\s+-(\d+\.\d+)\b')
DRIVERS_USE_PATTERN = re.compile(r'(?:from|import)\s+Drivers\.(\w+)')

# Vendor user-mode DLLs to bundle into the EXE when a given Drivers.* sub-driver
# is in use. Each entry: list of candidate paths to search on the build PC; the
# first hit gets bundled, plus any siblings listed in `companions` from the same
# directory. `install` names the vendor software that provides the DLL -- printed
# as an actionable hint when a used driver's DLL isn't found on the build PC, so
# it bakes in automatically on the next rebuild once that software is installed.
# Kernel-mode drivers (.sys/.inf) can't be bundled and must be installed on the
# target PC by the vendor's installer regardless.
DRIVER_BUNDLED_DLLS: dict[str, dict] = {}

# Third-party Python packages a given Drivers.* sub-driver needs at runtime that
# aren't a single bundle-able DLL -- they're full pip packages (their own
# modules + data + binaries). When the sub-driver is in use AND the package is
# importable on the build PC, we --collect-all it so the EXE carries it. When
# it's NOT installed on the build PC we can't bundle it, so we warn and skip
# rather than fail the build -- that optional feature just won't be baked in.
DRIVER_PYTHON_PACKAGES: dict[str, list[str]] = {}


def _sanitize(s: str) -> str:
    return s.replace("'", "").replace('"', "").replace("\\", "").strip()[:128]


def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def detect_app_name() -> str:
    return PROJECT_ROOT.name


def detect_entry_scripts() -> list[Path]:
    candidates: list[Path] = []
    for ext in ("*.py", "*.pyw"):
        for p in PROJECT_ROOT.glob(ext):
            if ENTRY_PATTERN.search(_read(p)):
                candidates.append(p)
    if not candidates:
        raise SystemExit(
            f"No entry script in {PROJECT_ROOT}. "
            "Need a top-level .py/.pyw with `if __name__ == \"__main__\":`."
        )
    if len(candidates) == 1:
        return candidates
    target = _norm(PROJECT_ROOT.name)
    name_matches = sorted(
        (c for c in candidates if _norm(c.stem) == target),
        key=lambda p: 0 if p.suffix == ".pyw" else 1,
    )
    if name_matches:
        return [name_matches[0]]
    for fb in ("app.pyw", "main.pyw", "app.py", "main.py"):
        for c in candidates:
            if c.name == fb:
                return [c]
    return sorted(candidates, key=lambda p: p.name)


def detect_packages() -> list[str]:
    return sorted(
        c.name for c in PROJECT_ROOT.iterdir()
        if c.is_dir() and (c / "__init__.py").exists()
    )


def detect_drivers_subpackages() -> list[str]:
    drivers = PROJECT_ROOT.parent / "Drivers"
    if not (drivers / "__init__.py").exists():
        return []
    used: set[str] = set()
    for ext in ("*.py", "*.pyw"):
        for p in PROJECT_ROOT.rglob(ext):
            if "builder" in p.parts or "__pycache__" in p.parts:
                continue
            for m in DRIVERS_USE_PATTERN.finditer(_read(p)):
                used.add(m.group(1))
    return sorted(used)


def detect_version(entry: Path) -> str:
    m = TITLE_PATTERN.search(_read(entry))
    if m:
        return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
    for p in sorted(PROJECT_ROOT.rglob("*.py")):
        if "builder" in p.parts or "__pycache__" in p.parts:
            continue
        m = TITLE_PATTERN.search(_read(p))
        if m:
            return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
    return "1.0.0"


def detect_bundled_dlls(drivers_subs: list[str]) -> tuple[list[Path], list[str]]:
    """Vendor DLLs to --add-binary for the used sub-drivers.

    Returns (found, missing_subs): the DLL paths present on the build PC (safe
    to bundle) and the sub-drivers whose bundle-able DLL wasn't found anywhere
    (reported as an actionable warning, not a build failure).
    """
    found: list[Path] = []
    seen: set[Path] = set()
    missing: list[str] = []
    for sub in drivers_subs:
        spec = DRIVER_BUNDLED_DLLS.get(sub)
        if not spec:
            continue
        primary_path: Path | None = None
        for cand in spec["primary"]:
            p = Path(cand)
            if p.is_file():
                primary_path = p
                break
        if primary_path is None:
            missing.append(sub)
            continue
        if primary_path not in seen:
            found.append(primary_path)
            seen.add(primary_path)
        for comp_name in spec["companions"]:
            comp = primary_path.parent / comp_name
            if comp.is_file() and comp not in seen:
                found.append(comp)
                seen.add(comp)
    return found, missing


def detect_collect_packages(drivers_subs: list[str]) -> tuple[list[str], list[str]]:
    """Third-party packages to --collect-all for the used sub-drivers.

    Returns (importable, missing): the ones present on the build interpreter
    (safe to --collect-all) and the ones a used driver wants but that aren't
    installed (can't be bundled -- reported as a warning, not a build failure).
    """
    import importlib.util
    want: list[str] = []
    for sub in drivers_subs:
        for pkg in DRIVER_PYTHON_PACKAGES.get(sub, ()):
            if pkg not in want:
                want.append(pkg)
    importable, missing = [], []
    for pkg in want:
        try:
            present = importlib.util.find_spec(pkg) is not None
        except (ImportError, ValueError):
            present = False
        (importable if present else missing).append(pkg)
    return importable, missing


def detect_manifest() -> Path | None:
    p = HERE / "app.manifest"
    return p if p.is_file() else None


def detect_icon() -> Path | None:
    for name in ("icon.ico", f"{PROJECT_ROOT.name}.ico"):
        p = PROJECT_ROOT / name
        if p.exists():
            return p
    return None


def detect_description(entry: Path, app_name: str) -> str:
    text = _read(entry)
    m = re.search(r'^\s*"""(.+?)"""', text, re.DOTALL | re.MULTILINE)
    if not m:
        return app_name
    line = m.group(1).strip().splitlines()[0].strip()
    skip = ("shim", "compat", "launcher", "entry point", "entry-point")
    looks_like_filename = bool(re.fullmatch(r'[\w.\-]+\.pyw?', line))
    if line and not looks_like_filename and not any(w in line.lower() for w in skip):
        return line
    return app_name


def detect_required_python() -> str | None:
    run_bat = PROJECT_ROOT / "run.bat"
    if not run_bat.exists():
        return None
    m = PY_VER_PATTERN.search(_read(run_bat))
    return m.group(1) if m else None


def maybe_relaunch_with_required_python() -> None:
    if os.environ.get("_BUILDER_RELAUNCHED") == "1":
        return
    required = detect_required_python()
    if not required:
        return
    current = f"{sys.version_info.major}.{sys.version_info.minor}"
    if current == required:
        return
    print(f"Project's run.bat pins Python {required} (currently {current}).")
    print(f"Relaunching builder under `py -{required}`...")
    env = os.environ.copy()
    env["_BUILDER_RELAUNCHED"] = "1"
    try:
        rc = subprocess.call(
            ["py", f"-{required}", str(Path(__file__).resolve())], env=env
        )
    except FileNotFoundError:
        print(f"ERROR: Python {required} not found. Install it via the "
              f"Python launcher (https://www.python.org/downloads/).")
        sys.exit(1)
    sys.exit(rc)


def write_version_info(product_name: str, exe_basename: str, version: str,
                       description: str, target: Path) -> None:
    major, minor, patch = version.split(".")
    ver_tuple = f"({major}, {minor}, {patch}, 0)"
    ver_string = f"{version}.0"
    target.write_text(
        f"""# Auto-generated by build_exe.py -- overwritten on next build.
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={ver_tuple},
    prodvers={ver_tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(
        u'040904B0',
        [
          StringStruct(u'CompanyName', u'{_sanitize(COMPANY)}'),
          StringStruct(u'FileDescription', u'{_sanitize(description)}'),
          StringStruct(u'FileVersion', u'{ver_string}'),
          StringStruct(u'InternalName', u'{_sanitize(exe_basename)}'),
          StringStruct(u'LegalCopyright', u'{_sanitize(COPYRIGHT)}'),
          StringStruct(u'OriginalFilename', u'{_sanitize(exe_basename)}.exe'),
          StringStruct(u'ProductName', u'{_sanitize(product_name)}'),
          StringStruct(u'ProductVersion', u'{ver_string}'),
        ]),
    ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])]),
  ]
)
""",
        encoding="utf-8",
    )


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("Installing PyInstaller...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--upgrade", "pyinstaller"]
        )


def main() -> int:
    maybe_relaunch_with_required_python()

    app_name = detect_app_name()
    entries = detect_entry_scripts()
    packages = detect_packages()
    drivers_subs = detect_drivers_subpackages()
    bundled_dlls, missing_dll_subs = detect_bundled_dlls(drivers_subs)
    collect_pkgs, missing_pkgs = detect_collect_packages(drivers_subs)
    manifest = detect_manifest()
    icon = detect_icon()
    multi = len(entries) > 1

    ensure_pyinstaller()

    for sub in missing_dll_subs:
        hint = DRIVER_BUNDLED_DLLS[sub].get("install", "the vendor software")
        print(f"WARNING: driver '{sub}' has a bundle-able DLL, but it wasn't "
              f"found on this build PC, so it can't be baked in.\n"
              f"         Install it on this build PC, then rebuild -- it bakes "
              f"in automatically:\n"
              f"             {hint}")

    for pkg in missing_pkgs:
        print(f"WARNING: a used driver needs the Python package '{pkg}', but it "
              f"isn't installed on this build PC, so it can't be bundled.\n"
              f"         Bake it in by installing it under the build "
              f"interpreter, then rebuild:\n"
              f'             "{sys.executable}" -m pip install {pkg}')

    overall_rc = 0
    for i, entry in enumerate(entries, 1):
        version = detect_version(entry)
        description = detect_description(entry, app_name)
        if multi:
            exe_basename = f"{app_name} - {entry.stem} v{version}"
        else:
            exe_basename = f"{app_name} v{version}"

        print()
        print(f"== [{i}/{len(entries)}] {exe_basename} ==")
        print(f"  Entry:       {entry.name}")
        print(f"  Version:     {version}")
        print(f"  Packages:    {', '.join(packages) if packages else '(none)'}")
        if drivers_subs:
            print(f"  Drivers:     {', '.join(drivers_subs)}")
        if bundled_dlls:
            print(f"  DLLs:        {', '.join(p.name for p in bundled_dlls)}")
        if collect_pkgs:
            print(f"  Collect:     {', '.join(collect_pkgs)}")
        print(f"  Manifest:    {manifest.name if manifest else '(none)'}")
        print(f"  Icon:        {icon.name if icon else '(none)'}")
        print(f"  Description: {description}")

        version_info = HERE / "version_info.txt"
        write_version_info(app_name, exe_basename, version, description,
                           version_info)

        cmd = [
            sys.executable, "-m", "PyInstaller",
            "--onedir",
            "--windowed",
            "--noupx",
            "--clean",
            "--noconfirm",
            f"--name={exe_basename}",
            f"--version-file={version_info}",
            f"--distpath={HERE / 'dist'}",
            f"--workpath={HERE / 'build'}",
            f"--specpath={HERE}",
            f"--paths={PROJECT_ROOT}",
        ]
        if drivers_subs:
            cmd.append(f"--paths={PROJECT_ROOT.parent}")
            for sub in drivers_subs:
                cmd.append(f"--collect-submodules=Drivers.{sub}")
                cmd.append(f"--hidden-import=Drivers.{sub}")
        if icon:
            cmd.append(f"--icon={icon}")
        if manifest:
            cmd.append(f"--manifest={manifest}")
        for dll in bundled_dlls:
            cmd.append(f"--add-binary={dll}{os.pathsep}.")
        for pkg in collect_pkgs:
            cmd.append(f"--collect-all={pkg}")
        for pkg in packages:
            cmd.append(f"--collect-submodules={pkg}")
        for mod in DEFAULT_EXCLUDES:
            cmd.append(f"--exclude-module={mod}")
        cmd.append(str(entry))

        print("Running PyInstaller...")
        rc = subprocess.call(cmd)
        if rc != 0:
            overall_rc = rc
            print(f"  -> FAILED (exit {rc})")
            continue
        exe_path = HERE / "dist" / f"{exe_basename}.exe"
        if exe_path.exists():
            mb = exe_path.stat().st_size / (1024 * 1024)
            print(f"  -> Built: {exe_path.name} ({mb:.1f} MB)")

    return overall_rc


if __name__ == "__main__":
    raise SystemExit(main())
