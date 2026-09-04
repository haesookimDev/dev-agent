#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root on a dedicated Ubuntu worker" >&2
  exit 1
fi

if [[ ! -e /dev/kvm ]]; then
  echo "/dev/kvm is unavailable; enable virtualization in firmware" >&2
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  qemu-kvm libvirt-daemon-system libvirt-clients virtinst qemu-utils \
  cloud-image-utils ovmf openssh-client wireguard nftables ca-certificates

if ! id kelpie >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/kelpie --shell /usr/sbin/nologin kelpie
fi
usermod -aG libvirt,kvm kelpie
install -d -o kelpie -g kelpie -m 0700 \
  /var/lib/kelpie/images /var/lib/kelpie/runs /etc/kelpie
worker_binary="${KELPIE_WORKER_BINARY:-./kelpie-worker}"
if [[ ! -x "$worker_binary" ]]; then
  echo "worker binary not found: $worker_binary" >&2
  exit 1
fi
install -m 0755 "$worker_binary" /usr/local/bin/kelpie-worker
install -m 0644 infra/systemd/kelpie-worker.service /etc/systemd/system/kelpie-worker.service

systemctl enable --now libvirtd
systemctl daemon-reload
echo "Install /etc/kelpie/worker.env, configure WireGuard, then run: systemctl enable --now kelpie-worker"
