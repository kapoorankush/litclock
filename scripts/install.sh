#!/bin/bash
#
# LitClock E-Ink Display Installer
# For Raspberry Pi Zero WH with Waveshare 7.5" e-Paper HAT (V2)
#
# Usage: curl -sSL https://raw.githubusercontent.com/kapoorankush/litclock/master/scripts/install.sh | bash
#
# This script will:
# 1. Install system dependencies
# 2. Install BCM2835 driver for GPIO
# 3. Enable SPI interface
# 4. Clone the repository with submodules
# 5. Set up Python virtual environment
# 6. Install Python dependencies
# 7. Set up systemd services and timer
#
# After installation, reboot to start setup via web UI.
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Configuration
INSTALL_DIR="/home/pi/litclock"
REPO_URL="https://github.com/kapoorankush/litclock.git"
BCM2835_VERSION="1.75"
BCM2835_URL="https://www.airspayce.com/mikem/bcm2835/bcm2835-${BCM2835_VERSION}.tar.gz"

# Check if running on Raspberry Pi
check_raspberry_pi() {
    if ! grep -q "Raspberry Pi" /proc/cpuinfo 2>/dev/null; then
        log_warn "This doesn't appear to be a Raspberry Pi. Proceeding anyway..."
    else
        log_info "Raspberry Pi detected"
    fi
}

# Check if running as root for certain operations
check_sudo() {
    if [ "$EUID" -ne 0 ]; then
        log_info "Some commands require sudo. You may be prompted for your password."
    fi
}

# Install system packages
install_system_packages() {
    log_info "Updating package lists..."
    sudo apt update

    log_info "Installing system dependencies..."
    sudo apt install -y \
        git \
        python3 \
        python3-pip \
        python3-venv \
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
        wireless-tools \
        qrencode \
        jq

    log_info "System packages installed successfully"
}

# Install BCM2835 library for GPIO access
install_bcm2835() {
    log_info "Installing BCM2835 library v${BCM2835_VERSION}..."

    cd /tmp

    if [ -f "bcm2835-${BCM2835_VERSION}.tar.gz" ]; then
        rm "bcm2835-${BCM2835_VERSION}.tar.gz"
    fi
    if [ -d "bcm2835-${BCM2835_VERSION}" ]; then
        rm -rf "bcm2835-${BCM2835_VERSION}"
    fi

    wget "${BCM2835_URL}"
    tar zxvf "bcm2835-${BCM2835_VERSION}.tar.gz"
    cd "bcm2835-${BCM2835_VERSION}"

    ./configure
    make
    sudo make check
    sudo make install

    cd /tmp
    rm -rf "bcm2835-${BCM2835_VERSION}" "bcm2835-${BCM2835_VERSION}.tar.gz"

    log_info "BCM2835 library installed successfully"
}

# Enable NTP time sync
enable_ntp() {
    log_info "Enabling NTP time sync..."
    sudo timedatectl set-ntp true
    log_info "NTP time sync enabled"
}

# Configure journald to use volatile storage (RAM) to reduce SD card wear
setup_journald() {
    log_info "Configuring journald for volatile storage..."
    sudo mkdir -p /etc/systemd/journald.conf.d
    sudo tee /etc/systemd/journald.conf.d/litclock.conf > /dev/null <<EOF
[Journal]
Storage=volatile
RuntimeMaxUse=16M
EOF
    sudo systemctl restart systemd-journald
    log_info "Journald configured for volatile storage (16M max)"
}

# #343 — let the pi service account bind port 80 for control_server (no
# capability, so it never touches the unit's NoNewPrivileges/setuid-sudo wiring).
setup_unprivileged_ports() {
    log_info "Allowing control_server to bind port 80 (ip_unprivileged_port_start)..."
    sudo install -m 0644 -o root -g root \
        "$INSTALL_DIR/sysctl.d/30-litclock-unprivileged-ports.conf" \
        /etc/sysctl.d/30-litclock-unprivileged-ports.conf
    # Apply live so a bind works this boot without a reboot.
    sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80 > /dev/null 2>&1 || true
    # Read from /proc, not `sysctl -n` — sysctl (/usr/sbin) isn't on the pi
    # user's PATH, so the bare call returns empty and false-warns (#343 hw QA).
    _port_floor=$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null || echo "")
    if [ "$_port_floor" = "80" ]; then
        log_info "Port 80 bindable by non-root."
    else
        log_warn "Could not lower unprivileged-port floor to 80 (got ${_port_floor:-unknown}); a reboot will apply the installed sysctl drop-in."
    fi
}

# Enable SPI interface
enable_spi() {
    log_info "Enabling SPI interface..."

    BOOT_CONFIG=""
    if [ -f "/boot/config.txt" ]; then
        BOOT_CONFIG="/boot/config.txt"
    elif [ -f "/boot/firmware/config.txt" ]; then
        BOOT_CONFIG="/boot/firmware/config.txt"
    else
        log_error "Could not find boot config file"
        return 1
    fi

    if grep -q "^dtparam=spi=on" "$BOOT_CONFIG"; then
        log_info "SPI is already enabled"
    elif grep -q "^#dtparam=spi=on" "$BOOT_CONFIG"; then
        sudo sed -i 's/^#dtparam=spi=on/dtparam=spi=on/' "$BOOT_CONFIG"
        log_info "SPI enabled (was commented out)"
    else
        echo "dtparam=spi=on" | sudo tee -a "$BOOT_CONFIG" > /dev/null
        log_info "SPI enabled (added to config)"
    fi
}

# Check if device is Pi Zero/Zero 2 W and offer WiFi stability fixes
setup_wifi_stability() {
    MODEL=$(cat /proc/device-tree/model 2>/dev/null)
    if [[ ! "$MODEL" == *"Zero 2 W"* ]] && [[ ! "$MODEL" == *"Zero W"* ]]; then
        return 0
    fi

    echo ""
    log_warn "Raspberry Pi Zero W detected: $MODEL"
    echo ""
    echo "This model has a known WiFi stability issue that can cause the system to"
    echo "hang and become unreachable. Would you like to apply WiFi stability fixes?"
    echo ""
    echo "[Y] Yes, apply fixes (Recommended)"
    echo "[n] No, skip"
    echo ""
    read -p "Apply WiFi stability fixes? [Y/n]: " response
    if [[ "$response" =~ ^[Nn]$ ]]; then
        log_info "Skipping WiFi stability fixes"
        return 0
    fi

    log_info "Applying WiFi stability fixes..."

    # a. Driver parameters
    log_info "Configuring brcmfmac driver parameters..."
    echo "options brcmfmac roamoff=1 feature_disable=0x82000" | sudo tee /etc/modprobe.d/brcmfmac.conf > /dev/null

    # b. Disable WiFi power management persistently via rc.local
    log_info "Disabling WiFi power management..."
    if [ ! -f /etc/rc.local ]; then
        sudo bash -c 'cat > /etc/rc.local << EOF
#!/bin/bash
/usr/sbin/iwconfig wlan0 power off
exit 0
EOF'
        sudo chmod +x /etc/rc.local
    elif ! grep -q "iwconfig wlan0 power off" /etc/rc.local; then
        sudo sed -i '/^exit 0/i /usr/sbin/iwconfig wlan0 power off' /etc/rc.local
    fi

    # c. WiFi watchdog script
    # Single canonical copy at scripts/wifi-watchdog.sh (#245 M5 D8 — the
    # pre-M5 inline heredoc here was a DRY violation against the pi-gen
    # build path, and the M5 OV1 firstboot-fallback extension required
    # touching both copies. Now there's one script; install.sh, update.sh,
    # and pi-gen all install from it.
    log_info "Installing WiFi watchdog script..."
    sudo install -m 0755 -o root -g root \
        "$INSTALL_DIR/scripts/wifi-watchdog.sh" /usr/local/bin/wifi-watchdog.sh

    # d. Watchdog timer is set up later by setup_systemd()
    # e. M5 — Reset-WiFi helper (#245 D11). Invoked by
    #    /usr/lib/systemd/system/litclock-wifi-reset.service via /api/wifi/reset.
    log_info "Installing Reset-WiFi helper script..."
    sudo install -m 0755 -o root -g root \
        "$INSTALL_DIR/scripts/litclock-wifi-reset.sh" /usr/local/bin/litclock-wifi-reset.sh

    log_info "WiFi stability fixes applied successfully"
}

# Clone the repository
clone_repository() {
    log_info "Setting up installation directory..."

    # Create parent directory if needed
    PARENT_DIR=$(dirname "$INSTALL_DIR")
    if [ ! -d "$PARENT_DIR" ]; then
        sudo mkdir -p "$PARENT_DIR"
        sudo chown "$USER:$USER" "$PARENT_DIR"
    fi

    if [ -d "$INSTALL_DIR" ]; then
        log_warn "Directory $INSTALL_DIR already exists"
        read -p "Do you want to remove it and re-clone? (y/N): " response
        if [[ "$response" =~ ^[Yy]$ ]]; then
            rm -rf "$INSTALL_DIR"
        else
            log_info "Keeping existing directory, updating instead..."
            cd "$INSTALL_DIR"
            git pull
            git submodule update --init --recursive
            return 0
        fi
    fi

    log_info "Cloning repository..."
    git clone --recursive "$REPO_URL" "$INSTALL_DIR"

    log_info "Repository cloned successfully"
}

# Download the pinned quote-image set from GitHub Releases.
# Quote images are NOT stored in git (see issue #82) — they live as a
# release asset and are fetched here. Graceful on failure: the clock
# falls back to a time-only display if no images are present, so a
# flaky network during install doesn't brick the installation.
download_quote_images() {
    log_info "Fetching quote images..."

    if [ ! -x "$INSTALL_DIR/scripts/download_images.sh" ]; then
        log_warn "download_images.sh not found or not executable — skipping"
        return 0
    fi

    "$INSTALL_DIR/scripts/download_images.sh" --repo-root "$INSTALL_DIR" || {
        log_warn "Quote image download failed — clock will fall back to time-only display until the next update"
    }
}

# Set up Python virtual environment and install packages
setup_python_env() {
    log_info "Setting up Python virtual environment..."

    cd "$INSTALL_DIR"

    if [ -d "venv" ]; then
        log_warn "Virtual environment already exists, recreating..."
        rm -rf venv
    fi

    # --system-site-packages keeps the venv compatible with apt-provisioned
    # GPIO libs (python3-gpiozero / spidev / lgpio / pigpio). Mirrors the
    # pi-gen build at pi-gen/stage3/01-setup-app/00-run.sh (#214).
    python3 -m venv --system-site-packages venv
    source venv/bin/activate

    log_info "Installing Python packages..."
    pip install --upgrade pip
    # Filter apt-provisioned names out of requirements.txt — they are
    # reachable via --system-site-packages and attempting to pip-install
    # them triggers sdist compilation on a gcc-less image (#214).
    REQUIREMENTS_APT="$INSTALL_DIR/requirements-apt.txt"
    REQUIREMENTS_FILTERED=$(mktemp)
    if [ -f "$REQUIREMENTS_APT" ]; then
        EXCLUDE_RE=$(grep -vE '^[[:space:]]*(#|$)' "$REQUIREMENTS_APT" | sed 's/\./\\./g' | paste -sd'|')
        grep -vE "^(${EXCLUDE_RE})==" "$INSTALL_DIR/requirements.txt" > "$REQUIREMENTS_FILTERED"
    else
        cp "$INSTALL_DIR/requirements.txt" "$REQUIREMENTS_FILTERED"
    fi
    # --upgrade keeps re-runs of install.sh (e.g. user re-runs after pulling
    # new code) from silently skipping bumped pins. Fresh installs are
    # unaffected. Parity with update.sh (#321). Eager strategy intentionally
    # NOT used — see update.sh comment for the Flask-transitive rationale.
    pip install --upgrade -r "$REQUIREMENTS_FILTERED"
    rm -f "$REQUIREMENTS_FILTERED"

    deactivate

    log_info "Python environment set up successfully"
}

# Set up systemd services and timer
setup_systemd() {
    log_info "Setting up systemd services..."

    # Remove old cron jobs if present
    if crontab -l 2>/dev/null | grep -q "litclock"; then
        log_info "Removing old user cron job..."
        crontab -l 2>/dev/null | grep -v "litclock" | crontab -
    fi
    if sudo crontab -l 2>/dev/null | grep -q "wifi-watchdog"; then
        log_info "Removing old root wifi-watchdog cron job..."
        sudo crontab -l 2>/dev/null | grep -v "wifi-watchdog" | sudo crontab -
    fi

    # Copy service and timer files
    sudo cp "$INSTALL_DIR/systemd/litclock-splash.service" /etc/systemd/system/
    sudo cp "$INSTALL_DIR/systemd/litclock-firstboot.service" /etc/systemd/system/
    sudo cp "$INSTALL_DIR/systemd/litclock.service" /etc/systemd/system/
    sudo cp "$INSTALL_DIR/systemd/litclock.timer" /etc/systemd/system/
    sudo cp "$INSTALL_DIR/systemd/litclock-shutdown.service" /etc/systemd/system/
    # #209 — weekly auto-update + LKG heartbeat
    sudo cp "$INSTALL_DIR/systemd/litclock-update.service" /etc/systemd/system/
    sudo cp "$INSTALL_DIR/systemd/litclock-update.timer" /etc/systemd/system/
    sudo cp "$INSTALL_DIR/systemd/litclock-lkg.service" /etc/systemd/system/
    sudo cp "$INSTALL_DIR/systemd/litclock-lkg.timer" /etc/systemd/system/
    # #209 follow-up — LKG auto-revert consumer (bootcheck). Timer-driven.
    sudo cp "$INSTALL_DIR/systemd/litclock-bootcheck.service" /etc/systemd/system/
    sudo cp "$INSTALL_DIR/systemd/litclock-bootcheck.timer" /etc/systemd/system/
    # EPIC #383 PR2 (#388) — handoff last-resort completer (timer-driven oneshot)
    sudo cp "$INSTALL_DIR/systemd/litclock-handoff-fallback.service" /etc/systemd/system/
    sudo cp "$INSTALL_DIR/systemd/litclock-handoff-fallback.timer" /etc/systemd/system/
    # #245 M1 — Control PWA always-on management surface
    sudo cp "$INSTALL_DIR/systemd/litclock-control.service" /etc/systemd/system/

    # #245 M4 — Control PWA scoped sudo for system actions. Validate the
    # source file BEFORE installing — a malformed sudoers entry locks out
    # `sudo` system-wide. visudo -c -f exits non-zero on bad syntax.
    SUDOERS_SRC="$INSTALL_DIR/sudoers/020_litclock-control"
    if ! sudo visudo -c -f "$SUDOERS_SRC"; then
        log_error "020_litclock-control failed visudo validation; not installed"
        exit 1
    fi
    sudo install -m 0440 -o root -g root \
        "$SUDOERS_SRC" /etc/sudoers.d/020_litclock-control

    # #309 — NetworkManager dispatcher. Re-renders the e-ink corner QR when
    # wlan0's IP changes so the displayed address never lags after DHCP churn.
    # Mode 0755 root:root — NM ignores dispatcher scripts that are not exactly
    # this (group/world-writable scripts are silently skipped).
    NM_DISP_SRC="$INSTALL_DIR/scripts/nm-dispatcher/99-litclock-ip-change"
    if [ -f "$NM_DISP_SRC" ]; then
        sudo install -d -m 0755 /etc/NetworkManager/dispatcher.d
        sudo install -m 0755 -o root -g root \
            "$NM_DISP_SRC" /etc/NetworkManager/dispatcher.d/99-litclock-ip-change
    fi

    # #387 — root-owned privilege helpers installed outside the pi-writable repo
    # (pi cannot rewrite what runs as root): the sudo tz-wrapper, the
    # mark-collected helper the root NM dispatcher invokes (C1), and reset-setup.sh
    # + its lib/state.sh (run as root by litclock-prepare-for-gift.service, which
    # pi can `systemctl start` via 020).
    sudo install -d -m 0755 /usr/local/lib/litclock
    sudo install -d -m 0755 /usr/local/lib/litclock/lib
    sudo install -m 0755 -o root -g root \
        "$INSTALL_DIR/scripts/litclock-set-timezone" \
        /usr/local/lib/litclock/litclock-set-timezone
    sudo install -m 0755 -o root -g root \
        "$INSTALL_DIR/scripts/litclock-mark-collected.sh" \
        /usr/local/lib/litclock/litclock-mark-collected.sh
    sudo install -m 0755 -o root -g root \
        "$INSTALL_DIR/scripts/reset-setup.sh" \
        /usr/local/lib/litclock/reset-setup.sh
    sudo install -m 0644 -o root -g root \
        "$INSTALL_DIR/scripts/lib/state.sh" \
        /usr/local/lib/litclock/lib/state.sh

    # #241 — tmpfs heartbeat directory created on every boot
    sudo cp "$INSTALL_DIR/systemd/tmpfiles.d/litclock.conf" /etc/tmpfiles.d/
    sudo systemd-tmpfiles --create /etc/tmpfiles.d/litclock.conf \
        || log_warn "systemd-tmpfiles --create failed — /run/litclock may not exist until reboot"

    # Copy wifi-watchdog units if the watchdog script was installed
    if [ -f /usr/local/bin/wifi-watchdog.sh ]; then
        sudo cp "$INSTALL_DIR/systemd/wifi-watchdog.service" /etc/systemd/system/
        sudo cp "$INSTALL_DIR/systemd/wifi-watchdog.timer" /etc/systemd/system/
    fi

    # #245 M5 — Reset-WiFi service unit (D11). Always installed so /api/wifi/reset
    # can dispatch via systemctl; the helper script existence guard mirrors
    # wifi-watchdog above.
    if [ -f /usr/local/bin/litclock-wifi-reset.sh ]; then
        sudo cp "$INSTALL_DIR/systemd/litclock-wifi-reset.service" /etc/systemd/system/
    fi

    # #510 — Factory reset unit (fired by /api/system/reset). Type=oneshot, no
    # [Install]; the root-owned reset-setup.sh copy it runs is installed above.
    sudo cp "$INSTALL_DIR/systemd/litclock-reset.service" /etc/systemd/system/

    sudo systemctl daemon-reload

    # Enable splash (runs on every boot)
    sudo systemctl enable litclock-splash.service

    # Enable first-boot (will disable itself after setup completes)
    sudo systemctl enable litclock-firstboot.service

    # Enable the timer (fires every minute on the minute)
    sudo systemctl enable litclock.timer

    # Enable shutdown splash (displays message on shutdown/reboot)
    sudo systemctl enable litclock-shutdown.service

    # Enable wifi-watchdog timer if the watchdog script was installed
    if [ -f /usr/local/bin/wifi-watchdog.sh ]; then
        sudo systemctl enable wifi-watchdog.timer
    fi

    # #209/#241 — enable the weekly auto-update timer and the LKG poll timer.
    # litclock-lkg.service is now driven by litclock-lkg.timer (#241); the
    # service itself has no [Install] section so we don't enable it directly.
    sudo systemctl enable litclock-update.timer
    sudo systemctl enable litclock-lkg.timer
    # #209 follow-up — LKG auto-revert (bootcheck) poll timer.
    sudo systemctl enable litclock-bootcheck.timer

    # EPIC #383 PR2 (#388) — handoff fallback timer (litclock-handoff-fallback.service
    # is timer-driven + has no [Install] section, so it's not enabled directly).
    sudo systemctl enable litclock-handoff-fallback.timer

    # #245 M1 — Control PWA. ConditionPathExists=/etc/litclock/.setup-complete
    # gates startup; on a fresh image the unit waits until first-boot finishes.
    sudo systemctl enable litclock-control.service

    # #337 A2/A8 — on-boot location re-resolve oneshot. Best-effort: runs
    # after NetworkManager-wait-online, gated on .handoff-complete (skips
    # fresh-flash boots where setup_server is still resolving), gated inside
    # the script on WEATHER_LOCATION_MODE=auto (skips Specific-mode users).
    sudo systemctl enable litclock-reresolve-location.service

    log_info "Systemd services and timer set up"
}

# Test the display
test_display() {
    log_info "Testing display..."

    cd "$INSTALL_DIR"
    source venv/bin/activate

    # First try clearing the display
    if python3 src/clear.py 2>/dev/null; then
        log_info "Display cleared successfully"
    else
        log_warn "Could not clear display (may need reboot for SPI)"
    fi

    deactivate
}

# Print summary
print_summary() {
    echo ""
    echo "=========================================="
    echo "  LitClock Installation Complete"
    echo "=========================================="
    echo ""
    echo "Installation directory: $INSTALL_DIR"
    echo ""
    echo "Next steps:"
    echo ""
    echo "  Reboot to start setup via web UI:"
    echo "     sudo reboot"
    echo ""
    echo "  On boot, the display will guide you through WiFi and"
    echo "  configuration setup from your phone."
    echo ""
}

# Main installation flow
main() {
    echo ""
    echo "=========================================="
    echo "  LitClock E-Ink Display Installer"
    echo "=========================================="
    echo ""
    echo "This will install LitClock on your Raspberry Pi."
    echo "Installation directory: $INSTALL_DIR"
    echo ""

    read -p "Continue with installation? (Y/n): " response
    if [[ "$response" =~ ^[Nn]$ ]]; then
        echo "Installation cancelled."
        exit 0
    fi

    check_raspberry_pi
    check_sudo

    install_system_packages
    install_bcm2835
    enable_spi
    enable_ntp
    setup_journald
    setup_wifi_stability
    clone_repository
    setup_unprivileged_ports
    download_quote_images
    setup_python_env
    setup_systemd

    print_summary
}

# Run main function
main "$@"
