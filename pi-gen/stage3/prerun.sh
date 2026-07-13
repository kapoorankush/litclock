#!/bin/bash -e
# Copy rootfs from the previous stage (stage2) if not already present.
# This is required by pi-gen — without it, the stage has no rootfs to chroot into.

if [ ! -d "${ROOTFS_DIR}" ]; then
	copy_previous
fi
