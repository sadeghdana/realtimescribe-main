"""Package client/ as RealTran_client.zip for the other PCs, with this server's address filled
into the launchers (forward-slash paths and executable .sh, so it also unpacks correctly on macOS).

    python make_client_zip.py                         # address detected from this PC
    python make_client_zip.py --server-ip 10.0.0.5    # if this PC has several addresses
"""
import argparse
import getpass
import re
import stat
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKIP = {"client_settings.json", "start_client_local.bat"}  # the local launcher is for the server PC only
LAUNCHERS = {"start_client.bat", "start_client.sh", "start_client_lan.bat", "start_client_lan.sh"}

p = argparse.ArgumentParser(description="Build RealTran_client.zip for the PCs that connect to this server")
p.add_argument("--server-ip", help="this server's LAN address (default: detected)")
p.add_argument("--ssh-user", default=getpass.getuser(),
               help="account the SSH launcher logs in with (default: the current Windows user)")
args = p.parse_args()

ip = args.server_ip
if not ip:
    sys.path.insert(0, str(ROOT))
    from realtran.certs import local_ipv4s
    ips = local_ipv4s()
    if not ips:
        sys.exit("No LAN address found on this PC: pass --server-ip.")
    ip = ips[0]
    if len(ips) > 1:
        print(f"This PC has several addresses {ips}; using {ip} (choose another with --server-ip).")
fill = {"__SERVER_IP__": ip, "__SSH_USER__": args.ssh_user}

out = ROOT / "RealTran_client.zip"
out.unlink(missing_ok=True)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in sorted((ROOT / "client").iterdir()):
        if f.is_file() and f.name not in SKIP:
            data = f.read_bytes()
            if f.name in LAUNCHERS:
                text = data.decode("utf-8")
                for key, value in fill.items():
                    text = text.replace(key, value)
                data = text.encode("utf-8")
            info = zipfile.ZipInfo(f"client/{f.name}", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | (0o755 if f.suffix == ".sh" else 0o644)) << 16
            z.writestr(info, data)

with zipfile.ZipFile(out) as z:
    version = re.search(r'CLIENT_VERSION = "([^"]+)"', z.read("client/realtran_client.py").decode()).group(1)
    print(f"{out.name}: client {version}, {len(z.namelist())} files, server wss://{ip}:8765 "
          f"(SSH: {args.ssh_user}@{ip})")
