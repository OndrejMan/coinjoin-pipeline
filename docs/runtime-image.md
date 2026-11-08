# Runtime image

`ghcr.io/ondrejman/coinjoin-pipeline-runtime` is one commit of this repository with what it
needs to run: Python 3.12, the dependencies of `pyproject.toml`, and the sources in
`/workspace/coinjoin-pipeline`, run by `/usr/local/bin/coinjoin-pipeline` (and `cjp`) the way
`runIt.sh` runs a checkout. Nothing is installed as a wheel. It is not a wrapper the
pipeline runs in: the CLI still drives Docker, Kubernetes and PBS from the outside, and
images that drive the pipeline, such as cj-frontend, start `FROM` it.

## Publishing

`Publish Runtime Image` (`.github/workflows/publish-runtime-image.yaml`) runs after `Tests`
passes for a push to `main` and publishes the tested commit as `:<commit>` and `:latest`.
Commits with `[skip ci]` get no image. cj-frontend pins the image by digest and Dependabot
there opens a pull request for each new digest.

## A checkout over the image's copy

A checkout mounted over `/workspace/coinjoin-pipeline` replaces the code and keeps the
dependencies, so development needs no rebuild until `pyproject.toml` gains a dependency.

## The commit in the manifest

`research_manifest.json` records `pipeline_source`:

| `origin`   | when                                          | `commit`                                | `dirty`             |
| ---------- | --------------------------------------------- | --------------------------------------- | ------------------- |
| `checkout` | the tree has `.git` (a checkout, or mounted)  | `git rev-parse HEAD`, null if git fails | uncommitted changes |
| `image`    | no `.git`, `COINJOIN_PIPELINE_GIT_COMMIT` set | the commit the image was built from     | `false`             |
| `unknown`  | no `.git` and no commit (a local build)       | null                                    | null                |

A checkout git cannot read (another owner, a worktree whose `.git` points outside the
container) records a null commit, never the image's.

## Local build

```bash
docker build -f container/runtime.Dockerfile --build-arg REVISION=$(git rev-parse HEAD) -t coinjoin-pipeline-runtime:local .
```

Leave out `REVISION` for a tree with uncommitted changes; the manifest then says `unknown`.
