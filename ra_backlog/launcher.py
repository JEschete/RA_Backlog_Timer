"""Launch a registered ROM in an emulator.

Security note. The dashboard is served on localhost, and *any* page open in
your browser can POST to localhost. An endpoint that spawns local processes is
therefore reachable by every site you visit, so this module is deliberately
narrow:

* it never accepts a path from a request -- only an `ra_id`, resolved against
  the `launch_targets` table you populated yourself;
* registration validates that the file exists and sits under an allowed root;
* the emulator is resolved from a small allowlist, not from user input;
* arguments are passed as a list, never through a shell.

The remaining exposure is that a malicious page could start a game you already
registered. That is annoying rather than dangerous, and the alternative -- no
launching at all -- was worse.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import log

# Emulators we are willing to start, and how to hand them a ROM.
# `{rom}` and `{core}` are substituted; nothing else is interpolated.
KNOWN_EMULATORS: dict[str, list[str]] = {
    "retroarch": ["-L", "{core}", "{rom}"],
    "mesen": ["{rom}"],
    "duckstation": ["{rom}"],
    "pcsx2": ["{rom}"],
    "dolphin": ["-e", "{rom}"],
    "ppsspp": ["{rom}"],
    "bizhawk": ["{rom}"],
    "snes9x": ["{rom}"],
    "mgba": ["{rom}"],
}

ROM_SUFFIXES = {
    ".nes", ".sfc", ".smc", ".gb", ".gbc", ".gba", ".n64", ".z64", ".v64",
    ".md", ".gen", ".smd", ".bin", ".cue", ".chd", ".iso", ".gcm", ".rvz",
    ".nds", ".pbp", ".cso", ".32x", ".sms", ".gg", ".pce", ".ws", ".wsc",
    ".zip", ".7z",
}


class LaunchError(RuntimeError):
    """Raised when a launch is refused. The message is safe to show a user."""


@dataclass(frozen=True)
class LaunchRoots:
    """Directories ROMs may live under. Empty means "no launching at all"."""
    roots: tuple[Path, ...] = ()

    @classmethod
    def from_strings(cls, values) -> "LaunchRoots":
        resolved = []
        for v in values or ():
            try:
                p = Path(v).expanduser().resolve()
            except (OSError, ValueError):
                continue
            if p.is_dir():
                resolved.append(p)
        return cls(tuple(resolved))

    def contains(self, candidate: Path) -> bool:
        if not self.roots:
            return False
        try:
            resolved = candidate.resolve()
        except OSError:
            return False
        for root in self.roots:
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
        return False


def validate_rom(path: str, roots: LaunchRoots) -> Path:
    """Check a ROM path at *registration* time, when a human is present."""
    try:
        candidate = Path(path).expanduser().resolve()
    except (OSError, ValueError) as exc:
        raise LaunchError(f"Unusable path: {exc}") from exc

    if not candidate.exists():
        raise LaunchError(f"No such file: {candidate}")
    if not candidate.is_file():
        raise LaunchError("That is a directory, not a ROM file")
    if candidate.suffix.lower() not in ROM_SUFFIXES:
        raise LaunchError(f"Unexpected file type: {candidate.suffix}")
    if not roots.contains(candidate):
        raise LaunchError(
            "That file is outside your configured ROM folders. Add its folder "
            "to the ROM roots first.")
    return candidate


def resolve_emulator(name: str | None) -> tuple[str, list[str]]:
    """Map a name to an executable and its argument template."""
    key = (name or "retroarch").strip().lower()
    if key not in KNOWN_EMULATORS:
        raise LaunchError(
            f"Unknown emulator {key!r}. Known: {', '.join(sorted(KNOWN_EMULATORS))}")

    executable = _find_executable(key)
    if executable is None:
        raise LaunchError(f"Could not find {key} on your PATH")
    return executable, KNOWN_EMULATORS[key]


def _find_executable(name: str) -> str | None:
    import shutil
    for candidate in (name, f"{name}.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def build_command(rom: Path, emulator: str | None, core: str | None,
                  extra_args: str | None) -> list[str]:
    executable, template = resolve_emulator(emulator)

    if "{core}" in " ".join(template) and not core:
        raise LaunchError(f"{emulator} needs a core; none is registered for this game")

    command = [executable]
    for part in template:
        if part == "{rom}":
            command.append(str(rom))
        elif part == "{core}":
            command.append(str(core))
        else:
            command.append(part)

    if extra_args:
        # Parsed as argv, never handed to a shell.
        command.extend(shlex.split(extra_args))
    return command


def launch(target: dict, roots: LaunchRoots) -> list[str]:
    """Start a registered target. Returns the command for logging/display."""
    if not target:
        raise LaunchError("No ROM registered for this game")

    rom = Path(target["rom_path"]).expanduser()
    if not rom.exists():
        raise LaunchError(f"Registered ROM has moved or been deleted: {rom}")
    if not roots.contains(rom):
        # Re-checked at launch: the roots may have been narrowed since registration.
        raise LaunchError("Registered ROM is no longer inside an allowed folder")

    command = build_command(rom, target.get("emulator"), target.get("core"),
                            target.get("extra_args"))

    log.info("Launching: %s", " ".join(command))
    try:
        kwargs: dict = {"close_fds": True}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(command, **kwargs)      # noqa: S603 - allowlisted argv
    except OSError as exc:
        raise LaunchError(f"Could not start the emulator: {exc}") from exc

    return command
