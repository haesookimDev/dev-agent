#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

# First installation only. A logging-policy upgrade requires a separate
# maintenance/rollback procedure, not overwriting a live privileged hook.
if [[ "$(id -u)" != 0 || "${KELPIE_NETWORK_HOOK_INSTALL_ACK:-}" != dedicated-idle-host-only || "$#" != 1 ]]; then
  echo "requires root, dedicated-idle-host-only acknowledgement and a reviewed hook binary" >&2
  exit 1
fi
hook_binary="$1"
if [[ "$hook_binary" != /* || ! -f "$hook_binary" || -L "$hook_binary" || ! -x "$hook_binary" ]]; then
  echo "requires an absolute regular executable hook binary" >&2
  exit 1
fi
for parent in /etc /etc/libvirt /etc/libvirt/hooks /var /var/lib; do
  if [[ ! -d "$parent" || -L "$parent" || "$(stat -c '%u:%a' "$parent")" != 0:755 ]]; then
    echo "hook parent directories must be root-owned mode 0755" >&2
    exit 1
  fi
done
for target in /etc/libvirt/hooks/network /etc/libvirt/hooks/network.d /var/lib/kelpie-network-guard; do
  if [[ -e "$target" || -L "$target" ]]; then
    echo "existing network hooks or guard state require reviewed maintenance; nothing replaced" >&2
    exit 1
  fi
done
hook_domains="$(/usr/bin/virsh -c qemu:///system list --all --uuid)"
hook_networks="$(/usr/bin/virsh -c qemu:///system net-list --uuid)"
if [[ -n "$hook_domains" || -n "$hook_networks" ]]; then
  echo "requires no domains and no active networks before restarting libvirt" >&2
  exit 1
fi
/usr/sbin/nft --version >/dev/null
/usr/bin/systemctl is-active --quiet systemd-journald.service

install -d -o root -g root -m 0755 /var/lib/kelpie-network-guard /etc/libvirt/hooks/network.d
install -o root -g root -m 0755 "$hook_binary" /etc/libvirt/hooks/network.d/50-kelpie-egress
# libvirt discovers hooks at daemon startup. Never invoke libvirt inside the
# hook itself: that synchronous callback would deadlock the daemon.
/usr/bin/systemctl restart libvirtd.service
/usr/bin/systemctl is-active --quiet libvirtd.service
echo "Installed the scoped network logging hook; internet egress is not enabled by this installer."
