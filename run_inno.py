"""
run_inno.py — finds ISCC.exe and runs it on installer.iss.
Called by build.bat to avoid batch syntax issues with spaces/parens in paths.
"""
import sys
import os
import shutil
import subprocess
import winreg
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent


def find_iscc():
    # 1. PATH
    found = shutil.which("ISCC.exe")
    if found:
        return found

    # 2. Registry (works on any drive / install directory)
    reg_keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"),
        (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 5_is1"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 5_is1"),
    ]
    for hive, subkey in reg_keys:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                install_dir, _ = winreg.QueryValueEx(key, "InstallLocation")
                candidate = Path(install_dir) / "ISCC.exe"
                if candidate.exists():
                    return str(candidate)
        except OSError:
            pass

    return None


def main():
    iscc = find_iscc()
    if not iscc:
        print()
        print("=" * 44)
        print("  Inno Setup not found.")
        print("  Download (free) from:")
        print("  https://jrsoftware.org/isdl.php")
        print("  Install it, then re-run build.bat.")
        print("=" * 44)
        print()
        print("  The executable is already built at:")
        print(r"  dist\Conductor\Conductor.exe")
        print("  You can run it directly without installing.")
        sys.exit(2)

    print(f"  Found Inno Setup: {iscc}")
    iss = str(SCRIPT_DIR / "installer.iss")
    result = subprocess.run([iscc, iss])
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
