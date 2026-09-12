#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

valid_approved_sha256() {
  [[ "$1" =~ ^[0-9a-f]{64}$ ]]
}

trusted_directory() {
  local path="$1" trusted_uid="$2" metadata uid mode links
  [[ -d "$path" && ! -L "$path" ]] || return 1
  metadata="$(/usr/bin/stat --format='%u:%a:%h' -- "$path" 2>/dev/null)" || return 1
  IFS=: read -r uid mode links <<<"$metadata"
  [[ "$uid" == 0 || "$uid" == "$trusted_uid" ]] || return 1
  [[ "$mode" =~ ^[0-7]+$ && "$links" =~ ^[1-9][0-9]*$ ]] || return 1
  (( (8#$mode & 0022) == 0 ))
}

trusted_path_chain() {
  local path="$1" trusted_uid="$2" canonical relative current="" index
  local -a components
  [[ "$path" == /* && "$path" != *$'\n'* && "$path" != *$'\r'* && "$path" != *$'\t'* ]] || return 1
  canonical="$(/usr/bin/realpath --canonicalize-existing -- "$path" 2>/dev/null)" || return 1
  [[ "$canonical" == "$path" ]] || return 1
  trusted_directory / "$trusted_uid" || return 1
  relative="${path#/}"
  IFS=/ read -r -a components <<<"$relative"
  ((${#components[@]} > 0)) || return 1
  for ((index = 0; index < ${#components[@]} - 1; index++)); do
    current+="/${components[index]}"
    trusted_directory "$current" "$trusted_uid" || return 1
  done
}

trusted_hook_source() {
  local path="$1" trusted_uid="$2" metadata uid mode links
  trusted_path_chain "$path" "$trusted_uid" || return 1
  [[ -f "$path" && ! -L "$path" && -x "$path" ]] || return 1
  metadata="$(/usr/bin/stat --format='%u:%a:%h' -- "$path" 2>/dev/null)" || return 1
  IFS=: read -r uid mode links <<<"$metadata"
  [[ "$uid" == "$trusted_uid" && "$links" == 1 && "$mode" =~ ^[0-7]+$ ]] || return 1
  (( (8#$mode & 0022) == 0 ))
}

trusted_staging_parent() {
  local path="$1" trusted_uid="$2" canonical
  [[ "$path" == /* ]] || return 1
  canonical="$(/usr/bin/realpath --canonicalize-existing -- "$path" 2>/dev/null)" || return 1
  [[ "$canonical" == "$path" ]] || return 1
  trusted_path_chain "$path" "$trusted_uid" || return 1
  trusted_directory "$path" "$trusted_uid"
}

hook_sha256() {
  local output
  output="$(/usr/bin/sha256sum -- "$1" 2>/dev/null)" || return 1
  printf '%s\n' "${output%% *}"
}

stage_hook_binary() {
  local source="$1" approved_sha256="$2" stage_parent="$3" trusted_uid="$4"
  local stage_directory staged_binary staged_sha256
  valid_approved_sha256 "$approved_sha256" || return 1
  trusted_hook_source "$source" "$trusted_uid" || return 1
  trusted_staging_parent "$stage_parent" "$trusted_uid" || return 1
  stage_directory="$(/usr/bin/mktemp --directory -- "$stage_parent/.kelpie-network-hook-install.XXXXXXXX" 2>/dev/null)" || return 1
  staged_binary="$stage_directory/network-hook"
  if ! /usr/bin/chmod 0700 -- "$stage_directory" 2>/dev/null ||
    ! /usr/bin/cp --reflink=never -- "$source" "$staged_binary" >/dev/null 2>&1 ||
    ! /usr/bin/chmod 0700 -- "$staged_binary" 2>/dev/null ||
    ! trusted_hook_source "$staged_binary" "$trusted_uid"; then
    /usr/bin/rm -f -- "$staged_binary" >/dev/null 2>&1 || true
    /usr/bin/rmdir -- "$stage_directory" >/dev/null 2>&1 || true
    return 1
  fi
  staged_sha256="$(hook_sha256 "$staged_binary")" || {
    /usr/bin/rm -f -- "$staged_binary" >/dev/null 2>&1 || true
    /usr/bin/rmdir -- "$stage_directory" >/dev/null 2>&1 || true
    return 1
  }
  if [[ "$staged_sha256" != "$approved_sha256" ]]; then
    /usr/bin/rm -f -- "$staged_binary" >/dev/null 2>&1 || true
    /usr/bin/rmdir -- "$stage_directory" >/dev/null 2>&1 || true
    return 1
  fi
  printf '%s\n' "$staged_binary"
}

worker_quiescence_state() {
  local data="$1" line load_seen="" active_seen="" unit_file_seen="" pid_seen=""
  ((${#data} <= 1024)) && [[ "$data" != *$'\r'* ]] || return 1
  while IFS= read -r line; do
    case "$line" in
      LoadState=masked)
        [[ -z "$load_seen" ]] || return 1
        load_seen=1
        ;;
      ActiveState=inactive)
        [[ -z "$active_seen" ]] || return 1
        active_seen=1
        ;;
      UnitFileState=masked-runtime)
        [[ -z "$unit_file_seen" ]] || return 1
        unit_file_seen=1
        ;;
      MainPID=0)
        [[ -z "$pid_seen" ]] || return 1
        pid_seen=1
        ;;
      *) return 1 ;;
    esac
  done <<<"$data"
  [[ "$load_seen" == 1 && "$active_seen" == 1 && "$unit_file_seen" == 1 && "$pid_seen" == 1 ]]
}

read_worker_service_state() {
  /usr/bin/systemctl show --no-pager \
    --property=LoadState --property=ActiveState --property=UnitFileState --property=MainPID \
    kelpie-worker.service 2>/dev/null
}

install_conditions_safe() {
  local before="$1" domains="$2" networks="$3" after="$4"
  worker_quiescence_state "$before" && [[ -z "$domains" && -z "$networks" ]] &&
    worker_quiescence_state "$after"
}

install_host_quiescent() {
  local before domains networks after
  before="$(read_worker_service_state)" || return 1
  domains="$(/usr/bin/virsh -c qemu:///system list --all --uuid 2>/dev/null)" || return 1
  networks="$(/usr/bin/virsh -c qemu:///system net-list --uuid 2>/dev/null)" || return 1
  after="$(read_worker_service_state)" || return 1
  install_conditions_safe "$before" "$domains" "$networks" "$after"
}

install_network_hook_files() {
  local staged_binary="$1" approved_sha256="$2" installed_binary
  /usr/bin/install -d -o root -g root -m 0755 /var/lib/kelpie-network-guard /etc/libvirt/hooks/network.d || return 1
  /usr/bin/install -o root -g root -m 0755 "$staged_binary" /etc/libvirt/hooks/network.d/50-kelpie-egress || return 1
  installed_binary=/etc/libvirt/hooks/network.d/50-kelpie-egress
  if ! trusted_hook_source "$installed_binary" 0 || [[ "$(hook_sha256 "$installed_binary")" != "$approved_sha256" ]]; then
    echo "installed network hook does not match its approved source" >&2
    return 1
  fi
}

restart_libvirt_for_hook() {
  /usr/bin/systemctl restart libvirtd.service || return 1
  /usr/bin/systemctl is-active --quiet libvirtd.service
}

guarded_network_hook_install() {
  if ! install_host_quiescent; then
    echo "Worker runtime mask or empty libvirt inventory changed before hook installation" >&2
    return 1
  fi
  install_network_hook_files "$@" || return 1
  if ! install_host_quiescent; then
    echo "Worker runtime mask or empty libvirt inventory changed before libvirt restart" >&2
    return 1
  fi
  restart_libvirt_for_hook
}

main() {
# First installation only. A logging-policy upgrade requires a separate
# maintenance/rollback procedure, not overwriting a live privileged hook.
if [[ "$(/usr/bin/id -u)" != 0 || "${KELPIE_NETWORK_HOOK_INSTALL_ACK:-}" != dedicated-idle-host-only || "$#" != 2 ]]; then
  echo "requires root, dedicated-idle-host-only acknowledgement, a reviewed hook binary and its approved SHA256" >&2
  exit 1
fi
hook_binary="$1"
approved_sha256="$2"
if ! valid_approved_sha256 "$approved_sha256" || ! trusted_hook_source "$hook_binary" 0; then
  echo "requires a canonical root-owned private single-link hook binary and an approved SHA256" >&2
  exit 1
fi
for parent in /etc /etc/libvirt /etc/libvirt/hooks /var /var/lib; do
  if [[ ! -d "$parent" || -L "$parent" || "$(/usr/bin/stat -c '%u:%a' "$parent")" != 0:755 ]]; then
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
if ! install_host_quiescent; then
  echo "requires an inactive runtime-masked kelpie-worker service, no domains and no active networks" >&2
  exit 1
fi
/usr/sbin/nft --version >/dev/null
/usr/bin/systemctl is-active --quiet systemd-journald.service

staged_binary="$(stage_hook_binary "$hook_binary" "$approved_sha256" /var/lib 0)" || {
  echo "reviewed hook source could not be pinned to its approved SHA256" >&2
  exit 1
}
staging_directory="${staged_binary%/*}"
cleanup_staging() {
  /usr/bin/rm -f -- "$staged_binary" >/dev/null 2>&1 || true
  /usr/bin/rmdir -- "$staging_directory" >/dev/null 2>&1 || true
}
trap cleanup_staging EXIT

# libvirt discovers hooks at daemon startup. Never invoke libvirt inside the
# hook itself: that synchronous callback would deadlock the daemon.
guarded_network_hook_install "$staged_binary" "$approved_sha256" || exit 1
echo "Installed the scoped network logging hook; internet egress is not enabled by this installer."
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
