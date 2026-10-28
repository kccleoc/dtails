#!/usr/bin/env python3
"""Entry point run inside the dtails-build container (see Dockerfile).

Rebuilds AppState from a JSON state file the host wrote, then runs the exact
same build pipeline (src/runner.py) the GUI and CLI already use — just with
this container's pinned tool versions instead of whatever squashfs-tools/
mtools/dosfstools happen to be on the host, so mksquashfs/mkfs.fat output is
byte-identical no matter which machine invokes the container.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path("/work")
sys.path.insert(0, str(REPO))

from src.state import AppState                # noqa: E402
from src.runner import run_selected_actions_stream  # noqa: E402
import src.runner as runner                   # noqa: E402

# The Tails rootfs is amd64. On a foreign-arch host (e.g. Apple Silicon) the
# chroot's amd64 binaries only run through the VM's binfmt emulator, whose
# interpreter lives outside the chroot and would fail to resolve. Bind the
# interpreter into the chroot for exactly the duration of the chroot step and
# remove every trace afterwards, so the repacked squashfs is byte-identical to
# a native-amd64 build.

_BINFMT_DIR = Path("/proc/sys/fs/binfmt_misc")


def _ensure_binfmt_mounted() -> None:
    """binfmt_misc is not mounted in the container's mount namespace by
    default; mounting it (privileged) exposes the VM kernel's global
    entries, including the Rosetta interpreter registration."""
    try:
        if _BINFMT_DIR.is_dir() and any(_BINFMT_DIR.iterdir()):
            return
        if not _BINFMT_DIR.is_dir():
            _BINFMT_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["sudo", "mount", "-t", "binfmt_misc", "binfmt_misc", str(_BINFMT_DIR)],
            check=True, capture_output=True,
        )
    except Exception:
        pass


def _amd64_interpreter_paths() -> list:
    _ensure_binfmt_mounted()
    out = []
    try:
        entries = sorted(_BINFMT_DIR.iterdir())
    except OSError:
        return out
    for entry in entries:
        if entry.name in ("register", "status"):
            continue
        try:
            fields = entry.read_text().splitlines()
        except OSError:
            continue
        kv = {}
        for line in fields:
            if ":" in line:
                k, v = line.split(":", 1)
                kv[k.strip()] = v.strip()
            elif line.startswith(("interpreter", "magic")):
                k, v = line.split(None, 1)
                kv[k] = v
        magic = kv.get("magic", "")
        interp = kv.get("interpreter", "")
        # ELF64 with e_machine == x86-64 (0x3e at byte offset 18).
        if (interp and magic.startswith("7f454c4602010100")
                and len(magic) >= 40 and magic[36:40] == "3e00"
                and interp not in out):
            out.append(interp)
    return out


def _bind_interpreters_into_chroot(cwd):
    root = Path(cwd) / "system_to_edit"
    binds = []
    for interp in _amd64_interpreter_paths():
        src = Path(interp)
        target = root / interp.lstrip("/")
        if not src.exists() or target.exists():
            continue
        subprocess.run(["sudo", "mkdir", "-p", str(target)], check=True)
        subprocess.run(["sudo", "mount", "--bind", interp, str(target)], check=True)
        binds.append((target, interp))
    if not binds:
        return None

    def cleanup():
        for target, _src in binds:
            subprocess.run(["sudo", "umount", str(target)], check=False)
            subprocess.run(["sudo", "rmdir", str(target)], check=False)
            parent = target.parent
            while parent != root:
                if subprocess.run(["sudo", "rmdir", str(parent)],
                                  check=False).returncode != 0:
                    break
                parent = parent.parent

    return cleanup


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: container_driver.py <state.json>", file=sys.stderr)
        return 2

    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))

    state = AppState()
    state.options_json = data.get("options_json") or {}
    state.selected_image = data.get("selected_image") or ""
    state.selected_device = data.get("selected_device") or {}
    state.selected_additions = data.get("selected_additions") or []
    state.selected_deletions = data.get("selected_deletions") or []
    state.version_overrides = data.get("version_overrides") or {}

    if not state.selected_image:
        print("[ERROR] state.json has no selected_image", file=sys.stderr)
        return 2

    def sink(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    _orig_chroot = runner._chroot_execute_script

    def chroot_with_interpreters(logs, chroot_sink, cwd):
        cleanup = _bind_interpreters_into_chroot(cwd)
        try:
            _orig_chroot(logs, chroot_sink, cwd)
        finally:
            if cleanup is not None:
                cleanup()

    runner._chroot_execute_script = chroot_with_interpreters

    run_selected_actions_stream(state, state.selected_image, sink=sink, cwd=str(REPO))
    return 0


if __name__ == "__main__":
    sys.exit(main())
