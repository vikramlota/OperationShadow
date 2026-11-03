#!/bin/bash
# Enable SSH on SCOUT01 - Run this script ON THE SCOUT01 ROVER
# Usage: sudo LAPTOP_IP=<LAPTOP_IP> SCOUT_IP=<SCOUT_IP> bash enable_scout_ssh.sh [LAPTOP_IP] [SCOUT_IP]

echo "=== SCOUT01 SSH Enablement Script ==="
echo ""

LAPTOP_IP="${1:-${LAPTOP_IP:-}}"
SCOUT_IP="${2:-${SCOUT_IP:-<SCOUT_IP>}}"

# Check if running with sudo
if [ "$EUID" -ne 0 ]; then 
    echo "❌ This script needs sudo privileges."
    echo "Run: sudo bash enable_scout_ssh.sh <LAPTOP_IP> [SCOUT_IP]"
    exit 1
fi

if [ -z "$LAPTOP_IP" ]; then
    echo "❌ Missing control laptop IP."
    echo "Usage: sudo bash enable_scout_ssh.sh <LAPTOP_IP> [SCOUT_IP]"
    echo "   or: sudo LAPTOP_IP=<LAPTOP_IP> bash enable_scout_ssh.sh"
    exit 1
fi

echo "1. Checking if SSH server is installed..."
if ! command -v sshd &> /dev/null; then
    echo "   SSH server not found. Installing openssh-server..."
    apt update
    apt install -y openssh-server
else
    echo "   ✅ SSH server is installed"
fi

echo ""
echo "2. Starting SSH service..."
systemctl start ssh
systemctl enable ssh

echo ""
echo "3. Checking SSH service status..."
if systemctl is-active --quiet ssh; then
    echo "   ✅ SSH service is running"
else
    echo "   ❌ SSH service failed to start"
    systemctl status ssh
    exit 1
fi

echo ""
echo "4. Configuring firewall (UFW)..."
echo "   Allowing SSH from control laptop: $LAPTOP_IP"
ufw allow from "$LAPTOP_IP" to any port 22 proto tcp
ufw reload

echo ""
echo "5. Verifying SSH is listening on port 22..."
if netstat -tlnp | grep -q ':22'; then
    echo "   ✅ SSH is listening on port 22"
else
    echo "   ⚠️  SSH may not be listening properly"
fi

echo ""
echo "=== Configuration Complete ==="
echo ""
echo "SSH should now be accessible from control laptop ($LAPTOP_IP)"
echo ""
echo "Test from control laptop:"
echo "  ssh <ROVER_USER>@$SCOUT_IP"
echo ""
