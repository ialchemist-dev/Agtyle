#!/usr/bin/env python
"""Install the pinned official Cedar CLI.

The binary is downloaded from the official cedar-policy release for the exact pinned version,
verified against the checksum the project publishes alongside it, and only then installed. There
is no `curl | sh` path: an unverified archive is discarded and any existing valid installation is
left untouched.

Usage:
    python scripts/install_cedar.py [--force] [--prefix .tools/cedar]
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agtyle.config import CEDAR_PINNED_VERSION  # noqa: E402

RELEASE_TAG: Final = f"cedar-policy-cli-v{CEDAR_PINNED_VERSION}"
BASE_URL: Final = f"https://github.com/cedar-policy/cedar/releases/download/{RELEASE_TAG}"
DOWNLOAD_TIMEOUT_SECONDS: Final = 120

#: Only these host combinations have an official prebuilt CLI asset.
TARGETS: Final[dict[tuple[str, str], tuple[str, str]]] = {
    ("darwin", "arm64"): ("aarch64-apple-darwin", "tar.xz"),
    ("darwin", "aarch64"): ("aarch64-apple-darwin", "tar.xz"),
    ("darwin", "x86_64"): ("x86_64-apple-darwin", "tar.xz"),
    ("linux", "aarch64"): ("aarch64-unknown-linux-gnu", "tar.xz"),
    ("linux", "arm64"): ("aarch64-unknown-linux-gnu", "tar.xz"),
    ("linux", "x86_64"): ("x86_64-unknown-linux-gnu", "tar.xz"),
    ("linux", "amd64"): ("x86_64-unknown-linux-gnu", "tar.xz"),
    ("windows", "amd64"): ("x86_64-pc-windows-msvc", "zip"),
    ("windows", "x86_64"): ("x86_64-pc-windows-msvc", "zip"),
}


class InstallError(RuntimeError):
    """A clear, actionable failure. Never a partially installed binary."""


def detect_target() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    try:
        return TARGETS[(system, machine)]
    except KeyError as exc:
        supported = ", ".join(sorted(f"{os}/{arch}" for os, arch in TARGETS))
        raise InstallError(
            f"no official Cedar {CEDAR_PINNED_VERSION} CLI build for {system}/{machine}. "
            f"Supported hosts: {supported}. Refusing to download a binary for another platform."
        ) from exc


def binary_name() -> str:
    return "cedar.exe" if platform.system().lower() == "windows" else "cedar"


def installed_version(binary: Path) -> str | None:
    """Return the installed version, or ``None`` if the binary is missing or unusable."""
    if not binary.is_file():
        return None
    try:
        completed = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip().split()[-1] if completed.stdout.strip() else None


def download(url: str, destination: Path) -> None:
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            destination.write_bytes(response.read())
    except urllib.error.URLError as exc:
        raise InstallError(f"could not download {url}: {exc.reason}") from exc


def expected_checksum(url: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=".sha256", delete=False) as handle:
        checksum_path = Path(handle.name)
    try:
        download(url, checksum_path)
        text = checksum_path.read_text(encoding="utf-8").strip()
    finally:
        checksum_path.unlink(missing_ok=True)
    if not text:
        raise InstallError(f"checksum file at {url} was empty")
    digest = text.split()[0].strip()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest.lower()
    ):
        raise InstallError(f"checksum file at {url} did not contain a SHA-256 digest")
    return digest.lower()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_binary(archive: Path, extension: str, workspace: Path) -> Path:
    target = workspace / "unpacked"
    target.mkdir(parents=True, exist_ok=True)
    if extension == "zip":
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(target)
    else:
        with tarfile.open(archive, "r:xz") as bundle:
            bundle.extractall(target, filter="data")
    candidates = sorted(target.rglob(binary_name()))
    if not candidates:
        raise InstallError(f"archive {archive.name} did not contain a {binary_name()} executable")
    return candidates[0]


def install(prefix: Path, *, force: bool) -> Path:
    triple, extension = detect_target()
    destination_dir = prefix / CEDAR_PINNED_VERSION
    destination = destination_dir / binary_name()

    current = installed_version(destination)
    if current == CEDAR_PINNED_VERSION and not force:
        print(f"cedar {current} already installed at {destination}")
        return destination

    asset = f"cedar-policy-cli-{triple}.{extension}"
    archive_url = f"{BASE_URL}/{asset}"
    checksum_url = f"{archive_url}.sha256"

    with tempfile.TemporaryDirectory(prefix="agtyle-cedar-") as workspace_name:
        workspace = Path(workspace_name)
        archive = workspace / asset
        print(f"downloading {archive_url}")
        download(archive_url, archive)

        expected = expected_checksum(checksum_url)
        actual = sha256_of(archive)
        if actual != expected:
            # An existing valid installation must survive a failed verification.
            raise InstallError(
                f"checksum mismatch for {asset}: expected {expected}, got {actual}. "
                "The download was discarded and any existing installation was left in place."
            )
        print(f"verified sha256:{actual}")

        extracted = extract_binary(archive, extension, workspace)
        destination_dir.mkdir(parents=True, exist_ok=True)
        staged = destination_dir / f".{binary_name()}.incoming"
        shutil.copy2(extracted, staged)
        staged.chmod(0o755)

        staged_version = installed_version(staged)
        if staged_version != CEDAR_PINNED_VERSION:
            staged.unlink(missing_ok=True)
            raise InstallError(
                f"downloaded binary reports version {staged_version!r}, expected "
                f"{CEDAR_PINNED_VERSION!r}; installation aborted"
            )
        # Replace atomically so a concurrent reader never sees a half-written binary.
        staged.replace(destination)

    final_version = installed_version(destination)
    if final_version != CEDAR_PINNED_VERSION:
        raise InstallError(f"installed cedar reports {final_version!r} after installation")
    print(f"installed cedar {final_version} at {destination}")
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prefix",
        type=Path,
        default=REPO_ROOT / ".tools" / "cedar",
        help="installation prefix (default: .tools/cedar)",
    )
    parser.add_argument(
        "--force", action="store_true", help="reinstall even when the pinned version is present"
    )
    arguments = parser.parse_args(argv)
    try:
        install(arguments.prefix, force=arguments.force)
    except InstallError as failure:
        print(f"error: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
