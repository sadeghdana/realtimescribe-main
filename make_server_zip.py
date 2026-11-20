"""Package the RealTran server for installing on another PC: RealTran_server.zip.

Only the program is included, from an explicit list: no models (setup_server.bat downloads them),
no Python environment, no meeting records, no certificates, nothing from this PC's configuration.
"""
import stat
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILES = [
    "README.md", "README_EN.md", "setup_server.bat", "download_models.py", "start.bat", "make_client_zip.py",
    "requirements.txt", "glossary.txt", "app.py",
    *sorted(f"realtran/{p.name}" for p in (ROOT / "realtran").glob("*.py")),
    "web/index.html",
    "client/README.md", "client/README_EN.md", "client/realtran_client.py", "client/requirements.txt",
    "client/setup_env.bat", "client/setup_env.sh", "client/start_client_local.bat",
    "client/start_client_lan.bat", "client/start_client_lan.sh", "client/start_client.bat", "client/start_client.sh",
]

out = ROOT / "RealTran_server.zip"
out.unlink(missing_ok=True)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for name in FILES:
        info = zipfile.ZipInfo(f"RealTran/{name}", date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (stat.S_IFREG | (0o755 if name.endswith(".sh") else 0o644)) << 16
        z.writestr(info, (ROOT / name).read_bytes())
print(f"{out.name}: {len(FILES)} files, {out.stat().st_size / 1024:.0f} KB")
