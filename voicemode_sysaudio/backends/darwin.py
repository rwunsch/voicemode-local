"""macOS backend — best-effort (BlackHole + device switching).

macOS blocks system-output capture without a virtual driver. The intended setup:
install BlackHole, create a Multi-Output device (real speakers + BlackHole) so
you still hear audio, and an Aggregate device (mic + BlackHole) for capture; the
toggle switches the input/output device via `SwitchAudioSource`. Wiring is
fiddlier than Linux/Windows, so this backend currently reports the manual steps
rather than auto-toggling. See docs/audio/README.md.
"""
from ..model import Result


def _switch_cmd(device, kind="input"):
    return ["SwitchAudioSource", "-t", kind, "-s", device]


def apply(state, cfg, run=None):
    return Result(
        ok=False, state="unknown",
        detail=("macOS is best-effort: install BlackHole, create Multi-Output + "
                "Aggregate devices, then toggle with SwitchAudioSource "
                "(see docs/audio/README.md)."),
        platform="darwin")
