"""Download the models RealTran runs on into models/, from the official Hugging Face repositories,
and check every file against the hashes Hugging Face publishes for it.

    python download_models.py            # download what is missing, then verify everything
    python download_models.py --verify   # only verify the files already in models/

About 20 GB. A file that is already there and correct is not downloaded again, so the script can
be re-run after an interruption. Models copied from another PC (e.g. on a USB drive) into models/
are verified the same way. None of the repositories needs a Hugging Face account.
"""
import argparse
import hashlib
import sys
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

ROOT = Path(__file__).resolve().parent
# folder -> (repository, pinned commit: the exact version RealTran was tested with)
MODELS = {
    "whisper-large-v3": ("openai/whisper-large-v3", "06f233fe06e710322aca913c1bc4249a0d71fce1"),
    "whisper-large-v3-turbo": ("openai/whisper-large-v3-turbo", "41f01f3fe87f28c78e2fbf8b568835947dd65ed9"),
    "gemma-4-E4B-it": ("google/gemma-4-E4B-it", "ee0ef6023621cff504d758262d4e04895a5af4a2"),
}
# Apple Silicon Macs (--mlx): the same models converted for Apple's MLX framework, Gemma quantized to
# 4 bits: ~6 GB of memory in all instead of ~16 GB, and faster on the Mac's GPU.
MLX_MODELS = {
    "mlx-whisper-large-v3-turbo": ("mlx-community/whisper-large-v3-turbo", "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb"),
    "mlx-gemma-4-e4b-it-4bit": ("mlx-community/gemma-4-e4b-it-4bit", "475b9088d29754a3379866cf5aeb6b41acd313c2"),
}
# The single-file safetensors weights + configs / tokenizer; not the duplicate PyTorch / Flax / fp32 copies.
PATTERNS = ["*.json", "*.txt", "*.jinja", "model.safetensors", "model-*.safetensors", "weights.safetensors"]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 24):
            h.update(chunk)
    return h.hexdigest()


def git_sha1(path):
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def wanted_files(api, repo, commit):
    """({filename: official hash}, total GB) for the files PATTERNS selects."""
    from fnmatch import fnmatch
    info = api.model_info(repo, revision=commit, files_metadata=True)
    chosen = [s for s in info.siblings
              if "/" not in s.rfilename and any(fnmatch(s.rfilename, p) for p in PATTERNS)]
    return ({s.rfilename: (s.lfs.sha256 if s.lfs else s.blob_id) for s in chosen},
            sum(s.size or 0 for s in chosen) / 2**30)


def verify(folder, files):
    bad = []
    for name, official in sorted(files.items()):
        f = folder / name
        if not f.exists():
            bad.append(f"{name}: missing")
            continue
        actual = sha256(f) if len(official) == 64 else git_sha1(f)
        if actual != official:
            bad.append(f"{name}: does not match the official file")
    return bad


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--verify", action="store_true", help="only verify, download nothing")
    p.add_argument("--mlx", action="store_true", help="the Apple Silicon (MLX) models instead")
    args = p.parse_args()

    api = HfApi()
    problems = 0
    for folder_name, (repo, commit) in (MLX_MODELS if args.mlx else MODELS).items():
        folder = ROOT / "models" / folder_name
        files, size = wanted_files(api, repo, commit)
        if not args.verify:
            print(f"Downloading {repo} ({size:.1f} GB) -> models/{folder_name} ...", flush=True)
            snapshot_download(repo, revision=commit, local_dir=folder, allow_patterns=PATTERNS)
        print(f"Verifying {folder_name} ({len(files)} files) ...", flush=True)
        bad = verify(folder, files)
        for b in bad:
            print("   ", b)
        print("    OK" if not bad else f"    {len(bad)} problem(s)")
        problems += len(bad)
    if problems:
        print("\nSome files are missing or damaged. Run this script again (without --verify) to fix them.")
        sys.exit(1)
    print("\nAll models are in place and identical to the official releases.")


if __name__ == "__main__":
    main()
