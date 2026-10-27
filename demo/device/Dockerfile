# A fake access point for local demos: an SSH server whose `ap` login drops
# into a tiny device-style CLI (demo_cli.py). Never expose this container.
FROM debian:12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-server python3 \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/sshd /etc/demo-ap /var/lib/demo-ap \
 && groupadd demoap \
 && chgrp demoap /var/lib/demo-ap && chmod 0775 /var/lib/demo-ap \
 && useradd -m -G demoap -s /bin/bash labops \
 && useradd -m -G demoap -s /usr/local/bin/demo-ap-shell ap \
 && echo /usr/local/bin/demo-ap-shell >> /etc/shells

COPY demo_cli.py /usr/local/bin/demo-cli
COPY demo-ap-shell /usr/local/bin/demo-ap-shell
COPY entrypoint.sh /entrypoint.sh
COPY sshd_config /etc/ssh/sshd_config.d/10-demo.conf
RUN chmod 0755 /usr/local/bin/demo-cli /usr/local/bin/demo-ap-shell /entrypoint.sh

EXPOSE 22
# sshd must start as root to switch to the logged-in user; this image is a
# local demo target only and is never deployed.
# nosemgrep: dockerfile.security.missing-user-entrypoint.missing-user-entrypoint
ENTRYPOINT ["/entrypoint.sh"]
