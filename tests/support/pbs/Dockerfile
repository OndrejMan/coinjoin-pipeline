FROM rockylinux:9

ARG OPENPBS_VERSION=23.06.06
ARG APPTAINER_VERSION=1.5.1

RUN dnf -y install dnf-plugins-core epel-release \
    && dnf config-manager --set-enabled crb \
    && dnf -y install \
        autoconf automake bash cjson-devel expat-devel gcc gcc-c++ git \
        hwloc-devel libedit-devel libical-devel libtool libX11-devel \
        libXext-devel libXft-devel libXt-devel make ncurses-devel openssl-devel \
        perl postgresql-devel python3-devel rpm-build shadow-utils swig \
        tar tcl-devel tk-devel which \
    && dnf -y install \
        "https://github.com/apptainer/apptainer/releases/download/v${APPTAINER_VERSION}/apptainer-${APPTAINER_VERSION}-1.x86_64.rpm" \
    && dnf clean all

RUN curl -fsSL \
      "https://github.com/openpbs/openpbs/archive/refs/tags/v${OPENPBS_VERSION}.tar.gz" \
      -o /tmp/openpbs.tar.gz \
    && mkdir -p /tmp/openpbs \
    && tar -xzf /tmp/openpbs.tar.gz --strip-components=1 -C /tmp/openpbs \
    && cd /tmp/openpbs \
    && ./autogen.sh \
    && ./configure --prefix=/opt/pbs \
    && make -j"$(nproc)" \
    && make install \
    && rm -rf /tmp/openpbs /tmp/openpbs.tar.gz

RUN dnf -y install postgresql postgresql-contrib postgresql-server \
    && dnf clean all

RUN dnf -y install \
      "https://github.com/apptainer/apptainer/releases/download/v${APPTAINER_VERSION}/apptainer-suid-${APPTAINER_VERSION}-1.x86_64.rpm" \
    && apptainer config global --set "mksquashfs procs" 1 \
    && apptainer config global --set "mksquashfs mem" 1G \
    && dnf clean all

RUN ln -sf /usr/bin/apptainer /usr/local/bin/singularity \
    && useradd --create-home --shell /bin/bash pbsuser \
    && mkdir -p /var/spool/pbs /scratch /opt/local-pbs \
    && chmod 1777 /scratch

COPY entrypoint.sh /opt/local-pbs/entrypoint.sh
COPY local_scratch_hook.py /opt/local-pbs/local_scratch_hook.py
RUN chmod 0755 /opt/local-pbs/entrypoint.sh

ENV PATH="/opt/pbs/bin:/opt/pbs/sbin:${PATH}"
ENTRYPOINT ["/opt/local-pbs/entrypoint.sh"]
CMD ["sleep", "infinity"]
