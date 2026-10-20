# receiver.pkr.hcl - build the exo-v2v receiver image and register it as Exoscale
# templates, one per boot mode: <prefix>-<flavour>-uefi and <prefix>-<flavour>-legacy.
#
#   packer init template/
#   packer build -var flavour=linux   -var image_bucket=my-bucket template/
#   packer build -var flavour=windows -var image_bucket=my-bucket template/
#
# Run on Ubuntu 24.04 as a user with passwordless sudo: build-image.sh needs root
# and mmdebstrap, qemu-utils, gdisk, dosfstools, grub-pc-bin, grub-efi-amd64-signed,
# shim-signed. The API key comes from EXOSCALE_API_KEY / EXOSCALE_API_SECRET; it
# needs Compute (template registration) and Object Storage (the image upload).
# The bucket must exist in the first zone of `zones`.

packer {
  required_plugins {
    exoscale = {
      source  = "github.com/exoscale/exoscale"
      version = "= 0.5.2"
    }
  }
}

variable "flavour" {
  type        = string
  default     = "linux"
  description = "linux, or windows (adds virtio-win and rhsrvany for Windows guests)"
  validation {
    condition     = contains(["linux", "windows"], var.flavour)
    error_message = "The flavour must be linux or windows."
  }
}

variable "zones" {
  type        = list(string)
  default     = ["ch-gva-2"]
  description = "Zones to register the templates in; the image is uploaded to the first one"
}

variable "image_bucket" {
  type        = string
  description = "Existing Object Storage bucket for the image upload (removed after import)"
}

variable "template_prefix" {
  type    = string
  default = "exo-v2v"
}

variable "api_key" {
  type      = string
  default   = env("EXOSCALE_API_KEY")
  sensitive = true
}

variable "api_secret" {
  type      = string
  default   = env("EXOSCALE_API_SECRET")
  sensitive = true
}

locals {
  image   = "${path.root}/output/exo-v2v-${var.flavour}.qcow2"
  name    = "${var.template_prefix}-${var.flavour}"
  version = formatdate("YYYYMMDD", timestamp())
  guests  = var.flavour == "windows" ? "Windows guests" : "Linux guests"
}

# The image is assembled on the build host (a RAM-root initramfs on a hybrid
# BIOS/UEFI disk), not inside a VM: there is nothing to boot or connect to.
source "null" "image" {
  communicator = "none"
}

build {
  name    = "exo-v2v-receiver"
  sources = ["source.null.image"]

  provisioner "shell-local" {
    inline = [
      "mkdir -p ${path.root}/output",
      "sudo VERSION=${local.version} bash ${path.root}/build-image.sh ${var.flavour} ${local.image}",
      "sudo chown $(id -u):$(id -g) ${local.image} ${local.image}.md5",
    ]
  }

  # One image, two templates: the boot mode is a template property; Secure Boot
  # is chosen per instance (the image boots through Canonical-signed shim/GRUB).
  post-processors {
    post-processor "artifice" {
      files = [local.image]
    }
    post-processor "exoscale-import" {
      api_key                   = var.api_key
      api_secret                = var.api_secret
      image_bucket              = var.image_bucket
      template_zones            = var.zones
      template_name             = "${local.name}-uefi"
      template_description      = "exo-v2v receiver (${local.guests}, UEFI) ${local.version}"
      template_username         = "v2v"
      template_boot_mode        = "uefi"
      template_disable_password = true
      keep_input_artifact       = true # the image feeds both imports
    }
  }

  post-processors {
    post-processor "artifice" {
      files = [local.image]
    }
    post-processor "exoscale-import" {
      api_key                   = var.api_key
      api_secret                = var.api_secret
      image_bucket              = var.image_bucket
      template_zones            = var.zones
      template_name             = "${local.name}-legacy"
      template_description      = "exo-v2v receiver (${local.guests}, BIOS) ${local.version}"
      template_username         = "v2v"
      template_boot_mode        = "legacy"
      template_disable_password = true
      keep_input_artifact       = true # the image feeds both imports
    }
  }
}
