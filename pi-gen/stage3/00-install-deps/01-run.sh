#!/bin/bash
# Install system packages and BCM2835 library from source for GPIO/SPI access
set -e

on_chroot << 'CHROOT'
set -e

# --- System packages needed for Pillow compilation and runtime ---
# (mirrors scripts/install.sh; skip packages already in Bookworm Lite base)
apt-get update
apt-get install -y \
    python3-dev \
    ttf-wqy-zenhei \
    ttf-wqy-microhei \
    libopenjp2-7-dev \
    libjpeg-dev \
    zlib1g-dev \
    libfreetype6-dev \
    liblcms2-dev \
    libwebp-dev \
    tcl8.6-dev \
    tk8.6-dev \
    libharfbuzz-dev \
    libfribidi-dev \
    libxcb1-dev \
    jq

# --- BCM2835 library ---
BCM2835_VERSION="1.75"
BCM2835_URL="https://www.airspayce.com/mikem/bcm2835/bcm2835-${BCM2835_VERSION}.tar.gz"

cd /tmp
wget "${BCM2835_URL}"
tar zxf "bcm2835-${BCM2835_VERSION}.tar.gz"
cd "bcm2835-${BCM2835_VERSION}"

./configure
make
make install

# Cleanup source
cd /tmp
rm -rf "bcm2835-${BCM2835_VERSION}" "bcm2835-${BCM2835_VERSION}.tar.gz"
CHROOT
