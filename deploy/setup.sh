#!/usr/bin/env bash
# ============================================================
# JobHunter — Automated Server Setup Script (Ubuntu / Debian)
# ============================================================

set -e

echo "🚀 Starting JobHunter server setup..."

# 1. Update packages
sudo apt update && sudo apt upgrade -y

# 2. Install Python, build tools, and TeX Live (for PDF resumes)
echo "📦 Installing system dependencies..."
sudo apt install -y \
    python3 \
    python3-venv \
    python3-pip \
    git \
    curl \
    build-essential \
    texlive-latex-base \
    texlive-latex-extra \
    texlive-fonts-recommended

# 3. Create virtual environment
echo "🐍 Setting up Python virtual environment..."
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# 4. Create data and output directories
mkdir -p data output/resumes

# 5. Open firewall port 8080 (if ufw or iptables active)
if command -v ufw >/dev/null 2>&1; then
    echo "🛡️ Configuring UFW firewall for port 8080..."
    sudo ufw allow 8080/tcp || true
fi

# Oracle Cloud specific: allow port 8080 in iptables
if sudo iptables -L >/dev/null 2>&1; then
    sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8080 -j ACCEPT || true
    sudo netfilter-persistent save || true
fi

echo "✅ Setup complete! Test running with: .venv/bin/python -m src.main --poll-once"
echo "To run 24/7 in background, install the systemd service in deploy/jobhunter.service"
