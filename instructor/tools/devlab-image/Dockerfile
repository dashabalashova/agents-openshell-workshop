# syntax=docker/dockerfile:1
# Devlab image: JupyterLab + NVIDIA OpenShell (CLI + gateway v0.1.1) with a private Docker daemon.
# Needs a Linux host kernel with Landlock and a privileged container (it runs dockerd).
# Will not start on Docker Desktop for macOS.

FROM docker:29.4.0-dind@sha256:a6dd5322747a95cd8e3207bd8d415a8fd20ec34e9c00f06dc019cbd912013489 AS docker
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl iproute2 iptables kmod openssh-client tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=docker /usr/local/bin/ /usr/local/bin/
COPY --from=docker /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/

ARG OPENSHELL=https://github.com/NVIDIA/OpenShell/releases/download/v0.1.1
RUN curl -fsSL --retry 3 -o /tmp/cli.tgz $OPENSHELL/openshell-x86_64-unknown-linux-musl.tar.gz \
    && echo '822e8e462944366cb1b3d3b023be20a675016714666124b05ad8699a03c43143  /tmp/cli.tgz' | sha256sum -c - \
    && curl -fsSL --retry 3 -o /tmp/gw.tgz $OPENSHELL/openshell-gateway-x86_64-unknown-linux-gnu.tar.gz \
    && echo '297dd4464b1c9fe1a3385a1d10a2bbd6a8e96ebbd12c6635d3879b5fe346e008  /tmp/gw.tgz' | sha256sum -c - \
    && tar -xzf /tmp/cli.tgz -C /usr/local/bin && tar -xzf /tmp/gw.tgz -C /usr/local/bin \
    && rm /tmp/cli.tgz /tmp/gw.tgz

RUN pip install --no-cache-dir jupyterlab==4.6.4

ENV DOCKER_HOST=unix:///run/openshell-docker.sock \
    OPENSHELL_GATEWAY=devlab \
    XDG_CONFIG_HOME=/workspace/.openshell/config \
    XDG_STATE_HOME=/workspace/.openshell/state \
    XDG_DATA_HOME=/workspace/.openshell/data

COPY gateway.toml /etc/openshell/gateway.toml
COPY start.sh /usr/local/bin/start.sh

WORKDIR /workspace
EXPOSE 8888
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/local/bin/start.sh"]
