#!/usr/bin/env bash
# Script to build a .deb package for the Resource Dashboard

set -euo pipefail

VERSION="1.2-2"
PKG_NAME="resource-dashboard"
ARCH="all"
BUILD_DIR="${PKG_NAME}_${VERSION}_${ARCH}"

echo "Building Debian package for $PKG_NAME v$VERSION..."

# 1. Create directory structure
mkdir -p "$BUILD_DIR/DEBIAN"
mkdir -p "$BUILD_DIR/usr/share/$PKG_NAME"
mkdir -p "$BUILD_DIR/usr/bin"
mkdir -p "$BUILD_DIR/usr/share/applications"
mkdir -p "$BUILD_DIR/usr/share/icons/hicolor/scalable/apps"
mkdir -p "$BUILD_DIR/usr/lib/systemd/system"

# 2. Create control file
cat <<EOF > "$BUILD_DIR/DEBIAN/control"
Package: $PKG_NAME
Version: $VERSION
Architecture: $ARCH
Maintainer: Abhishek Durgude
Depends: python3, python3-gi, gir1.2-gtk-3.0, gir1.2-webkit2-4.0 | gir1.2-webkit2-4.1
Section: utils
Priority: optional
Description: Live system resource monitoring dashboard
 A native Linux desktop application and standalone HTTP server for live system
 resource monitoring. Tracks CPU, memory, GPU, disk I/O, and network activity.
EOF

# 3. Copy application files
cp resource_dashboard.py "$BUILD_DIR/usr/share/$PKG_NAME/"
cp resource_dashboard_app.py "$BUILD_DIR/usr/share/$PKG_NAME/"
cp dashboard.html "$BUILD_DIR/usr/share/$PKG_NAME/"
chmod +x "$BUILD_DIR/usr/share/$PKG_NAME/resource_dashboard_app.py"

# 4. Copy icon
cp resource_dashboard_icon.svg "$BUILD_DIR/usr/share/$PKG_NAME/"
cp resource_dashboard_icon.svg "$BUILD_DIR/usr/share/icons/hicolor/scalable/apps/${PKG_NAME}.svg"

# 4b. Install systemd unit for headless mode
cp packaging/resource-dashboard@.service "$BUILD_DIR/usr/lib/systemd/system/"

# 5. Create desktop file
cat <<EOF > "$BUILD_DIR/usr/share/applications/$PKG_NAME.desktop"
[Desktop Entry]
Version=1.0
Type=Application
Name=Resource Dashboard
GenericName=System Monitor
Comment=Live resource monitoring dashboard for CPU, GPU, Memory, Disk, and Network
Exec=/usr/bin/$PKG_NAME
Icon=$PKG_NAME
Terminal=false
Categories=System;Monitor;Utility;
Keywords=monitor;cpu;gpu;memory;dashboard;resources;nvidia;system;
StartupNotify=true
StartupWMClass=$PKG_NAME
EOF
chmod 644 "$BUILD_DIR/usr/share/applications/$PKG_NAME.desktop"

# 6. Create executable launcher in /usr/bin
cat <<EOF > "$BUILD_DIR/usr/bin/$PKG_NAME"
#!/usr/bin/env bash
exec python3 /usr/share/$PKG_NAME/resource_dashboard_app.py "\$@"
EOF
chmod +x "$BUILD_DIR/usr/bin/$PKG_NAME"

# 7. Build the package
dpkg-deb --build "$BUILD_DIR"

# Cleanup build directory
rm -rf "$BUILD_DIR"

echo "✅ Package built: ${BUILD_DIR}.deb"
