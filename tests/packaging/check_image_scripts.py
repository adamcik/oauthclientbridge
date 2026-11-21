"""Check script references after nix2container relocates copyToRoot packages.

Host-side entrypoint checks cannot catch store paths removed by image assembly.
The image manifest records which package roots remain at their original paths.
This check needs no container privileges; runtime smoke tests remain separate.
"""

import json
import re
import sys
from pathlib import Path

_STORE_ROOT = re.compile(r"/nix/store/[0-9a-z]{32}-[A-Za-z0-9+._?=-]+")


def check_image(image: Path) -> None:
    manifest = json.loads(image.read_text())
    paths = [entry for layer in manifest["layers"] for entry in layer["paths"]]
    assert manifest["image-config"]["Entrypoint"] == ["/bin/entrypoint"]
    assert not any("uwsgi" in entry["path"].lower() for entry in paths), image
    relocated = [
        Path(entry["path"]) for entry in paths if "rewrite" in entry.get("options", {})
    ]
    entrypoints = [root / "bin/entrypoint" for root in relocated]
    entrypoints = [script for script in entrypoints if script.is_file()]
    assert len(entrypoints) == 1, f"{image}: expected one relocated entrypoint"
    entrypoint = entrypoints[0].read_text()
    assert "umask 0007" in entrypoint, entrypoint
    assert '/bin/oauthclientbridge-web "$@"' in entrypoint, entrypoint
    assert any((root / "bin/oauthclientbridge").is_file() for root in relocated), image
    assert any((root / "run/asgi").is_dir() for root in relocated), image
    assert not any((root / "run/uwsgi").exists() for root in relocated), image
    retained = {
        entry["path"] for entry in paths if "rewrite" not in entry.get("options", {})
    }
    for entry in paths:
        for script in (Path(entry["path"]) / "bin").glob("*"):
            if not script.is_file():
                continue
            with script.open("rb") as source:
                if source.read(2) != b"#!":
                    continue
                references = set(_STORE_ROOT.findall(source.read().decode()))
            missing = references - retained
            assert not missing, (
                f"{image}: {script} references absent image roots: {sorted(missing)}"
            )


if __name__ == "__main__":
    for argument in sys.argv[1:]:
        check_image(Path(argument))
