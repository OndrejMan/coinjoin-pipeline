# Runner Docker image-store setting (k3d import fix)

## Why this exists

Docker 29 defaults to the **containerd image store**
(`docker info` shows `driver-type: io.containerd.snapshotter.v1`). With it enabled,
`docker save` emits an OCI-format tarball that `k3d image import` cannot load —
`ctr` inside the node fails with `content digest sha256:...: not found`, yet
`k3d image import` still exits 0. The locally-tagged images never land on the
cluster nodes, the job pod goes to `Init:ImagePullBackOff`, and the k3d-based
tests (e.g. `tests/test-kubernetes-s3-minio.sh`, `tests/test-kubernetes-k3d.sh`)
hang until the outer timeout kills them (`exit 124`).

Fix: disable the containerd image store on the runner so `docker save` produces
the classic tarball format k3d imports cleanly.

## Recorded ORIGINAL state (before the change)

Captured 2026-07-20 on the self-hosted runner:

- `/etc/docker/daemon.json`: **did not exist** (no file at all).
- `docker info` reported:
  - `Server Version: 29.6.2`
  - `Storage Driver: overlayfs`
  - `driver-type: io.containerd.snapshotter.v1`  <- containerd image store ON
  - `Docker Root Dir: /var/lib/docker`

## Apply the fix

```bash
# /etc/docker/daemon.json
{ "features": { "containerd-snapshotter": false } }
```

```bash
sudo cp <staged>/daemon.json /etc/docker/daemon.json
sudo systemctl restart docker
docker info | grep -i driver-type   # the containerd line should be GONE
```

After the restart the containerd-store images are no longer visible to the
classic store, so re-pull the bases (tests also do this automatically):

```bash
docker pull ghcr.io/ondrejman/coinjoin-pipeline:latest
docker pull ghcr.io/ondrejman/coinjoin-emulator:latest
```

## How to REVERT to the original state

Because the original had **no** `daemon.json`, reverting means deleting the file
(not editing it back to `true`):

```bash
sudo rm /etc/docker/daemon.json
sudo systemctl restart docker
docker info | grep -i driver-type   # should show io.containerd.snapshotter.v1 again
```

Then, as after any store switch, the images built/tagged under the classic store
won't be visible; re-pull as needed:

```bash
docker pull ghcr.io/ondrejman/coinjoin-pipeline:latest
docker pull ghcr.io/ondrejman/coinjoin-emulator:latest
```

Note: switching the image store does not delete either store's contents on disk;
it only changes which store Docker uses. Nothing under `/var/lib/docker` needs to
be removed to revert.
