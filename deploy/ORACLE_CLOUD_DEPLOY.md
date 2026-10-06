# ☁️ Oracle Cloud Always Free — 24/7 Deployment Guide

This guide walks you through setting up an **Always Free** Linux server on Oracle Cloud Infrastructure (OCI) to host JobHunter 24/7 with zero ongoing cost.

---

## Why Oracle Cloud Always Free?
- **Cost:** $0.00 / month forever (no surprise charges).
- **Resources:** Up to 4 OCPU, 24 GB RAM (Ampere A1 ARM) or 2 AMD micro VMs.
- **Uptime:** Never sleeps, never pauses.
- **Includes:** Persistent NVMe storage and a static public IPv4 address.

---

## Step 1: Create an Oracle Cloud Free Account

1. Go to [oracle.com/cloud/free](https://www.oracle.com/cloud/free/).
2. Sign up (select your home region closest to you, e.g., `India South (Hyderabad)` or `India West (Mumbai)`).
3. *Note: Oracle requires a credit/debit card for identity verification, but charges $0.*

---

## Step 2: Create Your "Always Free" VM Instance

1. In the Oracle Cloud Console, click **"Create a VM instance"** (Compute $\rightarrow$ Instances $\rightarrow$ Create Instance).
2. **Name:** `jobhunter-server`
3. **Image and Shape:**
   - **Image:** `Canonical Ubuntu 24.04 Minimal` or `Ubuntu 22.04`
   - **Shape:** Select **"Always Free Eligible"**
     - Either **VM.Standard.A1.Flex** (Ampere ARM, 2 OCPU, 12GB RAM) OR **VM.Standard.E2.1.Micro** (AMD, 1GB RAM).
4. **Networking:**
   - Primary VNIC: Keep defaults (creates a public subnet).
   - Assign a public IPv4 address: **Yes**.
5. **Add SSH Keys:**
   - Select **"Generate a key pair for me"** and click **"Save private key"** (`ssh-key.key`). Save this file on your PC.
6. Click **Create**. The instance will be ready in ~1–2 minutes. Copy your **Public IP Address**.

---

## Step 3: Open Port 8080 in Oracle Cloud Firewall

Oracle Cloud blocks incoming traffic by default. Open port 8080 for the dashboard:

1. On your instance page, click your **Subnet** link under Primary VNIC.
2. Click the **Default Security List**.
3. Under **Ingress Rules**, click **"Add Ingress Rules"**:
   - **Source CIDR:** `0.0.0.0/0`
   - **IP Protocol:** `TCP`
   - **Destination Port Range:** `8080`
   - **Description:** `JobHunter Web Dashboard`
4. Click **Add Ingress Rules**.

---

## Step 4: Connect to Your VM

From PowerShell on your PC:
```powershell
ssh -i "path\to\your\ssh-key.key" ubuntu@<YOUR_VM_PUBLIC_IP>
```

---

## Step 5: Deploy JobHunter (One-Command Setup)

Once connected via SSH inside your Ubuntu terminal, run:

### Option 1: Native Systemd (Recommended)

```bash
# 1. Clone your project or upload files
git clone <your-github-repo-url> JobHunter
cd JobHunter

# 2. Run the automated installer
chmod +x deploy/setup.sh
./deploy/setup.sh

# 3. Create your .env file
nano .env
# (Paste your GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, and TELEGRAM_CHAT_ID, then press Ctrl+O, Enter, Ctrl+X)

# 4. Enable and start 24/7 background service
sudo cp deploy/jobhunter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jobhunter

# 5. Check status
sudo systemctl status jobhunter
```

### Option 2: Docker / Docker Compose

If you prefer containers:
```bash
# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# Clone and run
git clone <your-github-repo-url> JobHunter
cd JobHunter
nano .env # (Add your API keys)
docker compose up -d --build
```

---

## Step 6: Verify Everything

1. **Dashboard:** Open your browser and navigate to:
   ```
   http://<YOUR_VM_PUBLIC_IP>:8080
   ```
2. **Telegram Alerts:** You will automatically receive a message on Telegram every 15 minutes whenever a job matching your profile ($\ge 70$ score) is detected, complete with tailored resume and referral pitch!
3. **Logs:** Check live activity anytime via SSH:
   ```bash
   sudo journalctl -u jobhunter -f
   ```
