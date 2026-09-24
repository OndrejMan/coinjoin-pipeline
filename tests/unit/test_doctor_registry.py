from pathlib import Path
import subprocess
from unittest import mock

import pytest

from coinjoin_pipeline.doctor import check


@pytest.mark.parametrize("runtime", ["docker", "podman"])
def test_registry_probe_uses_an_image_name_without_transport(runtime, tmp_path):
    reference = "ghcr.io/ondrejman/emulator-manager:latest"
    images = mock.Mock()
    images.as_dict.return_value = {"emulator": reference}
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 1)
        if command[1:3] == ["manifest", "inspect"]:
            # Podman rejects docker:// here before contacting the registry.
            return subprocess.CompletedProcess(command, int(command[-1] != reference))
        return subprocess.CompletedProcess(command, 0)

    with mock.patch("coinjoin_pipeline.doctor.shutil.which", return_value=f"/bin/{runtime}"), \
         mock.patch("coinjoin_pipeline.doctor.subprocess.run", side_effect=run):
        assert check(runtime, Path(tmp_path), images) == []
    assert [f"/bin/{runtime}", "manifest", "inspect", reference] in calls
