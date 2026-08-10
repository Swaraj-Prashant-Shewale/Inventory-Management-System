"""Package the application into a distributable Windows folder.

    .venv\\Scripts\\python.exe build_exe.py

Produces dist/InventoryManagementSystem/ containing the .exe and everything it needs.
Copy that folder to each warehouse PC along with a .env holding the database details.

A one-folder build is deliberate. A single .exe unpacks itself to a temp directory on
every launch, which on a warehouse PC with aggressive antivirus adds seconds to startup
and occasionally trips a false positive.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "InventoryManagementSystem"


def clean():
    for folder in ("build", "dist"):
        target = ROOT / folder
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    spec = ROOT / f"{NAME}.spec"
    if spec.exists():
        spec.unlink()


def build():
    separator = ";" if sys.platform.startswith("win") else ":"
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", NAME,
        "--windowed",                       # no console window behind the GUI
        "--add-data", f"ui{separator}ui",   # styles.qss and anything else under ui/
        # Peewee and psycopg2 are imported dynamically in places PyInstaller misses.
        "--hidden-import", "psycopg2",
        "--hidden-import", "playhouse.shortcuts",
        "--hidden-import", "playhouse.migrate",
        "--hidden-import", "peewee",
        # PySide6 modules pulled in only through QtCharts.
        "--hidden-import", "PySide6.QtCharts",
        "--hidden-import", "PySide6.QtSvg",
        # Document generation.
        "--hidden-import", "reportlab.graphics.barcode.code128",
        "--hidden-import", "barcode.writer",
        "--collect-data", "barcode",
        "--collect-data", "reportlab",
        # Trim the bundle: these ship with PySide6 but the app never touches them.
        "--exclude-module", "PySide6.QtWebEngineCore",
        "--exclude-module", "PySide6.QtWebEngineWidgets",
        "--exclude-module", "PySide6.Qt3DCore",
        "--exclude-module", "PySide6.QtMultimedia",
        "--exclude-module", "PySide6.QtQuick3D",
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "pytest",
        str(ROOT / "main.py"),
    ]
    print("Running PyInstaller…\n  " + " ".join(command[2:]) + "\n")
    result = subprocess.run(command, cwd=str(ROOT))
    if result.returncode != 0:
        raise SystemExit(f"PyInstaller failed with exit code {result.returncode}")


def stage_runtime_files():
    """Put .env, the CA certificate and a deployment note beside the executable."""
    target = ROOT / "dist" / NAME
    if not target.exists():
        raise SystemExit("dist folder is missing — did the build fail?")

    example = ROOT / ".env.example"
    if example.exists():
        shutil.copy2(example, target / ".env.example")

    # The CA must sit beside the .exe, not inside the bundle: config resolves relative
    # paths against the executable's folder so the file can be replaced without rebuilding.
    certs = ROOT / "certs"
    if (certs / "supabase-ca.crt").exists():
        (target / "certs").mkdir(exist_ok=True)
        shutil.copy2(certs / "supabase-ca.crt", target / "certs" / "supabase-ca.crt")
        print("Staged the Supabase CA certificate")
    else:
        print("WARNING: certs/supabase-ca.crt is missing — the deployed app will "
              "connect without verifying the server certificate.")

    (target / "README-DEPLOY.txt").write_text(
        "Inventory Management System\n"
        "===========================\n\n"
        "1. Copy this whole folder to the PC.\n"
        f"2. Copy .env.example to .env and fill in the database details.\n"
        "   Ask whoever set up the system for the connection settings — do NOT use the\n"
        "   postgres superuser on a shop-floor machine.\n"
        f"3. Run {NAME}.exe\n\n"
        "The first person to run it against an empty database creates the Owner\n"
        "account. Everyone after that signs in normally.\n\n"
        "Documents (invoices, challans, labels) are written to the 'documents' folder\n"
        "beside the executable.\n",
        encoding="utf-8",
    )
    print(f"\nStaged runtime files into {target}")


def report():
    target = ROOT / "dist" / NAME
    exe = target / f"{NAME}.exe"
    total = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    print("\n" + "=" * 60)
    print(f"Built: {exe}")
    print(f"Exists: {exe.exists()}")
    print(f"Bundle size: {total / (1024 * 1024):.0f} MB "
          f"({sum(1 for _ in target.rglob('*') if _.is_file())} files)")
    print("=" * 60)


if __name__ == "__main__":
    clean()
    build()
    stage_runtime_files()
    report()
