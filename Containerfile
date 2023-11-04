FROM docker.io/library/debian:12

RUN apt update && apt install -y --no-install-recommends \
    bzip2 \
    curl \
    procps \
    python3 \
    python3-pip

# Install restic
# https://github.com/restic/restic/releases
ARG RESTIC_VERSION=0.16.1
RUN curl -sSLO https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_amd64.bz2 && \
    curl -sSLO https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/SHA256SUMS && \
    sha256sum --check --ignore-missing SHA256SUMS && \
    bunzip2 restic_*.bz2 && \
    mv restic_* /usr/local/bin/restic && \
    chmod +x /usr/local/bin/restic


# Install dependencies
COPY requirements.txt /etc/requirements.txt
RUN pip3 install --break-system-packages -r /etc/requirements.txt

# Add source code
COPY kub-vol-bak.py /usr/local/bin/kub-vol-bak

CMD ["kub-vol-bak", "backup"]
