# ⚡ GitHub Actions 24/7 Cloud Deployment Guide

This guide walks you through running JobHunter automatically in GitHub's cloud **24/7 for 100% free with NO credit card required**.

---

## Why GitHub Actions?
- **Cost:** $0 / month permanently (2,000 free runner minutes included monthly).
- **No Credit Card:** No verification, no payment details required.
- **24/7 Monitoring:** Runs on an automated cron timer every 45 minutes in GitHub's cloud.
- **Full PDF Compilation:** Ubuntu cloud runners have `pdflatex` installed, so your resumes are compiled into ATS-ready PDFs and pushed directly to Telegram.
- **On-Demand Runs:** You can trigger a poll cycle anytime with a single click from the GitHub website or app.

---

## Step 1: Push JobHunter to a Private GitHub Repository

1. Go to [github.com/new](https://github.com/new).
2. **Repository name:** `JobHunter`
3. **Visibility:** Select **Private** (recommended since your profile data is in `config/profile.yaml`).
4. Click **Create repository**.
5. From PowerShell in your `c:\Users\salil\Projects\JobHunter` folder, run:
   ```powershell
   git remote add origin https://github.com/<your-github-username>/JobHunter.git
   git branch -M main
   git push -u origin main
   ```

---

## Step 2: Add Your 3 Secrets to GitHub

Your API keys stay secure in GitHub's encrypted secrets vault (never exposed in code).

1. In your GitHub repository, click **Settings** (gear icon at the top).
2. In the left sidebar, click **Secrets and variables** $\rightarrow$ **Actions**.
3. Under **Repository secrets**, click **New repository secret** and add the following 3 secrets:

| Secret Name | Value (from your `.env` file) |
|---|---|
| `GEMINI_API_KEY` | Your Gemini key (`AQ.Ab8RN6K3...`) |
| `TELEGRAM_BOT_TOKEN` | Your Telegram Bot Token (`8699245225:AAGVn...`) |
| `TELEGRAM_CHAT_ID` | Your Telegram Chat ID (`1015838946`) |

---

## Step 3: Trigger Your First Test Run

1. In your repository on GitHub, click the **Actions** tab at the top.
2. In the left sidebar, click **JobHunter Automated Poller**.
3. Click the **Run workflow** dropdown button on the right $\rightarrow$ click **Run workflow**.
4. The workflow will start running in the cloud:
   - Check out code
   - Install Python dependencies & TeX Live
   - Fetch postings from Greenhouse, Lever, Ashby, and SmartRecruiters
   - Score matching backend jobs with Gemini
   - Generate tailored resumes & referral pitches
   - Send notifications and PDF attachments to your Telegram!

---

## Step 4: Sit Back & Relax!

- The workflow will automatically trigger every 45 minutes 24/7.
- Whenever a high-match role drops, you'll receive a Telegram push notification on your phone with the score, apply link, tailored resume PDF, and referral pitch ready to copy-paste.
- You do **not** need to keep your laptop or PC on!
