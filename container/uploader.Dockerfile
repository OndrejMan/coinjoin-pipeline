# Small in-cluster helper image: prefix preflight and artifact upload only.
# No exporters and no wheel — the frontend stages exporters straight to S3, so
# nothing here can go stale relative to the checkout.
FROM alpine:3.20

ARG S5CMD_VERSION=2.3.0
ARG KUBECTL_VERSION=v1.31.2
ARG TARGETARCH

RUN apk add --no-cache bash ca-certificates curl \
    && arch="${TARGETARCH:-$(uname -m)}" \
    && case "$arch" in \
         amd64|x86_64) s5arch=64bit; kubearch=amd64 ;; \
         arm64|aarch64) s5arch=ARM64; kubearch=arm64 ;; \
         *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://github.com/peak/s5cmd/releases/download/v${S5CMD_VERSION}/s5cmd_${S5CMD_VERSION}_Linux-${s5arch}.tar.gz" \
      | tar -xz -C /usr/local/bin s5cmd \
    && curl -fsSL -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${kubearch}/kubectl" \
    && chmod 0755 /usr/local/bin/kubectl \
    && s5cmd version \
    && kubectl version --client

ENTRYPOINT ["/bin/bash"]
