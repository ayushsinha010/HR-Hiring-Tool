import glob
import os
import subprocess

FONT_SOURCE_DIR = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts")

_CHECK_PS = (
    "Add-Type -AssemblyName System.Drawing;"
    "$c = New-Object System.Drawing.Text.InstalledFontCollection;"
    "$names = $c.Families | ForEach-Object { $_.Name };"
    "if ($names -contains 'Open Sans' -and $names -contains 'Open Sans Light') "
    "{ Write-Output 'OK' } else { Write-Output 'MISSING' }"
)

# Uses the Windows Shell's special Fonts folder object (CopyHere) rather than
# hand-writing files into C:\Windows\Fonts + registry keys directly - this is
# the same mechanism as right-click -> Install, and Windows handles the
# registration correctly on its own. Requires elevation (writes a system folder).
_INSTALL_PS_TEMPLATE = (
    "$src = '{src_dir}';"
    "$shellFonts = (New-Object -ComObject Shell.Application).Namespace(0x14);"
    "Get-ChildItem -Path $src -Filter 'OpenSans-*.ttf' | ForEach-Object {{ $shellFonts.CopyHere($_.FullName) }};"
    "Start-Sleep -Seconds 2;"
)


def is_installed():
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", _CHECK_PS],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return (result.stdout or "").strip() == "OK"
    except Exception:
        return False


def install():
    """Triggers a Windows UAC prompt on this machine's desktop; the signed-in
    user must approve it. Only meaningful when the Flask process and the
    person using the browser are on the same machine (true for a local run)."""
    if not os.path.isdir(FONT_SOURCE_DIR) or not glob.glob(os.path.join(FONT_SOURCE_DIR, "OpenSans-*.ttf")):
        return False, f"Open Sans .ttf files not found at {FONT_SOURCE_DIR}."

    install_script = _INSTALL_PS_TEMPLATE.format(src_dir=FONT_SOURCE_DIR)
    encoded = install_script.replace('"', '\\"')
    elevate_cmd = (
        f'Start-Process powershell -ArgumentList \'-NoProfile\',\'-Command\',"{encoded}" '
        "-Verb RunAs -Wait"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", elevate_cmd],
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "Timed out waiting for the install to finish (or the prompt wasn't approved in time)."
    except Exception as exc:
        return False, f"Couldn't start the install: {exc}"

    if is_installed():
        return True, "Open Sans installed system-wide."
    return False, "Install ran but Open Sans still isn't visible system-wide - it may not have been approved, or a restart of LibreOffice/the machine may be needed."
