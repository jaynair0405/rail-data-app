#!/bin/bash
# SSH Tunnel Script for MySQL Connection
# This creates a secure tunnel to the remote MySQL server

echo "================================================"
echo "Starting SSH Tunnel for MySQL Connection"
echo "================================================"
echo ""
echo "Local Port:  3307 (your Mac)"
echo "Remote Port: 3306 (MySQL on server)"
echo "Server:      93.127.198.125"
echo ""
echo "Keep this terminal open while running the app!"
echo "Press Ctrl+C to stop the tunnel"
echo ""
echo "================================================"
echo ""

# Start SSH tunnel
# -L 3307:localhost:3306 = Forward local port 3307 to remote localhost:3306
# -N = Don't execute remote command (just tunnel)
# -v = Verbose (optional, remove for cleaner output)
ssh -L 3307:localhost:3306 railway@93.127.198.125 -N
