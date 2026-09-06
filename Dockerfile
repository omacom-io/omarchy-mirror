FROM docker.io/library/archlinux:base

# Arch supplies pacman/libalpm, vercmp, repo-add, and the current Arch keyring.
RUN pacman-key --init \
    && pacman-key --populate archlinux \
    && pacman -Syu --noconfirm --needed python python-pip gnupg rsync ca-certificates pacman-contrib fakeroot \
    && pacman -Scc --noconfirm \
    && useradd --uid 1000 --create-home mirror
WORKDIR /opt/source
COPY pyproject.toml ./
COPY mirror ./mirror
COPY scripts/smoke-pacman.py ./scripts/smoke-pacman.py
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir '.[r2]'
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    MIRROR_CONFIG=/etc/omarchy-mirror/config.json \
    TMPDIR=/var/lib/omarchy-mirror/tmp
RUN install -d -o 1000 -g 1000 /var/lib/omarchy-mirror /var/lib/omarchy-mirror/tmp
USER mirror
WORKDIR /var/lib/omarchy-mirror
ENTRYPOINT ["omarchy-mirror"]
CMD ["--help"]
