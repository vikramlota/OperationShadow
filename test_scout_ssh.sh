#!/bin/bash
# Test SSH connection to SCOUT01

echo "=== Testing SSH Connection to SCOUT01 ==="
echo ""

SCOUT_IP="10.241.171.41"

echo "1. Testing TCP connectivity to port 22..."
if timeout 3 bash -c "cat < /dev/null > /dev/tcp/$SCOUT_IP/22" 2>/dev/null; then
    echo "   ✅ Port 22 is open"
else
    echo "   ❌ Port 22 is closed or unreachable"
    echo "   → Run enable_scout_ssh.sh ON SCOUT01 first"
    exit 1
fi

echo ""
echo "2. Testing SSH handshake..."
timeout 5 ssh -o ConnectTimeout=2 -o StrictHostKeyChecking=no -o BatchMode=yes $SCOUT_IP "echo 'SSH OK'" 2>&1

if [ $? -eq 0 ]; then
    echo "   ✅ SSH connection successful!"
else
    echo "   ⚠️  SSH port is open but authentication is needed"
    echo "   → Try: ssh username@$SCOUT_IP"
    echo "   → Common usernames: ubuntu, scout, pi, nvidia, robot"
fi

echo ""
echo "=== Test Complete ==="
