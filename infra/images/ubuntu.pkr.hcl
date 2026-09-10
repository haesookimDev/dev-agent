packer {
  required_version = "= 1.16.0"
  required_plugins {
    qemu = {
      source  = "github.com/hashicorp/qemu"
      version = "= 1.1.6"
    }
  }
}

# Use build.py, not a production image directory or an existing VM.
variable "run_dir" {
  type = string
}

locals {
  manifest = jsondecode(file("${var.run_dir}/inputs/manifest.json"))
  payloads = concat([local.manifest.codex, local.manifest.browser], local.manifest.runner_wheels)
  arm64    = local.manifest.architecture == "arm64"
}

source "qemu" "ubuntu" {
  accelerator      = "kvm"
  machine_type     = local.arm64 ? "virt,gic-version=host" : "q35"
  qemu_binary      = local.arm64 ? "qemu-system-aarch64" : "qemu-system-x86_64"
  cpu_model        = "host"
  cpus             = 2
  memory           = 4096
  disk_size        = "40G"
  disk_image       = true
  use_backing_file = false
  format           = "qcow2"
  disk_interface   = "virtio"
  net_device       = "virtio-net"
  iso_url          = "${var.run_dir}/inputs/files/${local.manifest.base_image.file}"
  iso_checksum     = "sha256:${local.manifest.base_image.sha256}"
  output_directory = "${var.run_dir}/image"
  vm_name          = "kelpie.qcow2"
  headless         = true
  vnc_bind_address = "127.0.0.1"
  cdrom_interface  = local.arm64 ? "virtio-scsi" : "virtio"

  efi_boot          = local.arm64
  efi_firmware_code = local.arm64 ? "${var.run_dir}/firmware/AAVMF_CODE.no-secboot.fd" : ""
  efi_firmware_vars = local.arm64 ? "${var.run_dir}/firmware/AAVMF_VARS.fd" : ""
  efi_drop_efivars  = local.arm64

  ssh_username                 = "kelpie-builder"
  ssh_private_key_file         = "${var.run_dir}/build_key"
  ssh_timeout                  = "10m"
  ssh_disable_agent_forwarding = true
  shutdown_command             = "sudo -n python3 /tmp/kelpie-image/tooling/guest.py seal"
  shutdown_timeout             = "5m"

  qemuargs = concat([
    ["-smbios", "type=1,product=KelpieGoldenImageBuild"],
    # The plugin default hostfwd binds all interfaces; override it explicitly.
    ["-netdev", "user,id=user.0,hostfwd=tcp:127.0.0.1:{{ .SSHHostPort }}-:22"],
    ["-device", "virtio-serial"],
    ["-chardev", "socket,path=${var.run_dir}/qga.sock,server=on,wait=off,id=qga0"],
    ["-device", "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0"],
    ], local.arm64 ? [
    ["-device", "virtio-gpu-pci"],
    # qemuargs replaces all default -device values, including the seed CD controller.
    ["-device", "virtio-scsi-pci,id=seed-scsi"],
    ["-device", "scsi-cd,bus=seed-scsi.0,drive=cdrom0"],
  ] : [])

  cd_label = "cidata"
  cd_content = {
    "meta-data" = jsonencode({ "instance-id" = "kelpie-image-build", "local-hostname" = "kelpie-image-build" })
    "user-data" = "#cloud-config\n${jsonencode({
      disable_root    = true
      ssh_pwauth      = false
      package_update  = false
      package_upgrade = false
      users = [
        {
          name                = "kelpie-builder"
          lock_passwd         = true
          shell               = "/bin/bash"
          ssh_authorized_keys = [chomp(file("${var.run_dir}/build_key.pub"))]
        },
        { name = "kelpie", lock_passwd = true, shell = "/bin/bash" },
      ]
      write_files = [{
        path        = "/etc/sudoers.d/kelpie-image-builder"
        owner       = "root:root"
        permissions = "0440"
        content     = "kelpie-builder ALL=(ALL) NOPASSWD: ALL\n"
      }]
    })}\n"
  }
}

build {
  sources = ["source.qemu.ubuntu"]

  provisioner "shell" {
    inline = ["umask 077; mkdir /tmp/kelpie-image; mkdir /tmp/kelpie-image/files /tmp/kelpie-image/tooling"]
  }
  provisioner "file" {
    source      = "${var.run_dir}/inputs/manifest.json"
    destination = "/tmp/kelpie-image/manifest.json"
  }
  provisioner "file" {
    sources     = [for item in local.payloads : "${var.run_dir}/inputs/files/${item.file}"]
    destination = "/tmp/kelpie-image/files/"
  }
  provisioner "file" {
    sources     = [for name in ["guest.py", "prepare.py", "health.py", "codex_package.py", "kelpie-runner.service"] : "${var.run_dir}/tooling/${name}"]
    destination = "/tmp/kelpie-image/tooling/"
  }
  provisioner "shell" {
    inline  = ["sudo -n python3 /tmp/kelpie-image/tooling/guest.py install"]
    timeout = "45m"
  }
}
