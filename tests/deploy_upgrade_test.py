import os
import stat
import subprocess
from pathlib import Path


def test_upgrade_writes_plain_image_override(tmp_path: Path) -> None:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()

    image_ref = "ghcr.io/example/oauthclientbridge:latest"
    target_digest = "ghcr.io/example/oauthclientbridge@sha256:1234"
    quadlet_file = tmp_path / "oauthclientbridge-test.container"
    override_file = tmp_path / "image.conf"
    quadlet_file.write_text("[Container]\nImage=old\n", encoding="utf-8")

    _write_executable(
        fakebin / "sudo",
        """#!/usr/bin/env bash
exec "$@"
""",
    )
    _write_executable(
        fakebin / "systemctl",
        """#!/usr/bin/env bash
exit 0
""",
    )
    _write_executable(
        fakebin / "podman",
        f"""#!/usr/bin/env bash
if [ "$1" = "container" ] && [ "$2" = "exists" ]; then
  exit 1
fi
if [ "$1" = "pull" ] && [ "$2" = "{image_ref}" ]; then
  exit 0
fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ] && [ "$3" = "{image_ref}" ] && [ "$4" = "--format" ] && [ "$5" = "{{{{.Id}}}}" ]; then
  printf 'sha256:target\n'
  exit 0
fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ] && [ "$3" = "{image_ref}" ] && [ "$4" = "--format" ] && [ "$5" = "{{{{index .RepoDigests 0}}}}" ]; then
  printf '{target_digest}\n'
  exit 0
fi
if [ "$1" = "inspect" ] && [ "$2" = "oauthclientbridge-test" ] && [ "$3" = "--format" ] && [ "$4" = "{{{{.Image}}}}" ]; then
  printf 'sha256:target\n'
  exit 0
fi
if [ "$1" = "inspect" ] && [ "$2" = "oauthclientbridge-test" ] && [ "$3" = "--format" ] && [ "$4" = "{{{{.ImageName}}}}" ]; then
  printf '{target_digest}\n'
  exit 0
fi
printf 'unexpected podman args: %s\n' "$*" >&2
exit 1
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"

    subprocess.run(
        [
            "bash",
            "deploy/upgrade.sh",
            "--instance",
            "test",
            "--image",
            image_ref,
            "--quadlet-file",
            str(quadlet_file),
            "--image-override",
            str(override_file),
        ],
        check=True,
        cwd="/workspaces/oauthclientbridge",
        env=env,
    )

    assert override_file.read_text(encoding="utf-8").splitlines() == [
        "# Managed by deploy/upgrade.sh",
        f"# Requested image: {image_ref}",
        "# To roll back, set Image=<previous-ref> and restart oauthclientbridge-test.service.",
        "# Previous runtime image: <unknown>",
        "[Container]",
        f"Image={target_digest}",
    ]


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
