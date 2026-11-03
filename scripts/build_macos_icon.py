"""Rebuild the macOS icon container from the application PNG."""

from pathlib import Path
import subprocess
import tempfile


RESOURCES = (Path(__file__).resolve().parents[1] / "Chemotion Sample Manager.app"
             / "Contents" / "Resources")


def build():
    with tempfile.TemporaryDirectory(prefix="sample_manager-icon-") as directory:
        iconset = Path(directory) / "ChemotionSampleManager.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                suffix = "@2x" if scale == 2 else ""
                output = iconset / f"icon_{size}x{size}{suffix}.png"
                subprocess.run([
                    "sips", "--resampleHeightWidth", str(size * scale), str(size * scale),
                    str(RESOURCES / "chemotion-sample-manager-icon.png"), "--out", str(output),
                ], check=True, stdout=subprocess.DEVNULL)
        subprocess.run([
            "iconutil", "--convert", "icns", str(iconset),
            "--output", str(RESOURCES / "ChemotionSampleManager.icns"),
        ], check=True)


if __name__ == "__main__":
    build()
