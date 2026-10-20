"""Entry point of the Windows desktop client built on embeddable Python.

``horizon.exe`` (desktop/claude-code-tools/client-stub) runs this file with the
bundled interpreter, ``python.exe -I -s -X utf8 app\\horizon_main.py <args>``, and
passes the arguments through unchanged. It is the same CLI as the PyInstaller
entry point in horizon_client.py. Built by desktop/client/build-client-embed.ps1.
"""

import os
import site
from pathlib import Path

APP = Path(__file__).resolve().parent
SITE_PACKAGES = APP / "site-packages"

# The pinned dependencies are in site-packages. site.addsitedir also runs the
# .pth files there: pywin32.pth puts win32, win32\lib and pythonwin on sys.path,
# and pywin32_bootstrap registers the folder that holds pywintypes and
# pythoncom. The explicit call below makes the DLL folder available regardless.
site.addsitedir(str(SITE_PACKAGES))

# os.add_dll_directory only lasts as long as the object it returns, so keep it.
_DLL_DIRECTORIES = []
_PYWIN32_DLLS = SITE_PACKAGES / "pywin32_system32"
if _PYWIN32_DLLS.is_dir() and hasattr(os, "add_dll_directory"):
    _DLL_DIRECTORIES.append(os.add_dll_directory(str(_PYWIN32_DLLS)))

from horizon.cli.main import main  # noqa: E402

if __name__ == "__main__":
    main(prog_name="horizon")
