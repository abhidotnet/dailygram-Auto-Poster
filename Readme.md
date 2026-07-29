# DailyGram Auto-Poster

A simple, reliable, and ultra-low-cost Instagram automation built with **AWS Lambda, S3, and EventBridge Scheduler**.

Drop your images or videos into the `uploads/` folder in S3. Every day at 2:30 PM Eastern Time, the Lambda function automatically selects the next unprocessed file, publishes it to your Instagram account (Feed or Reels), and moves it to the `processed/` folder.

Perfect for creators who want true **set-it-and-forget-it** daily posting.

---

## Features

- Fully automated daily Instagram posting
- Batch upload once per month (or quarter)
- Automatic move from `uploads/` to `processed/`
- Uses Instagram Graph API (requires Business or Creator account)
- Stores access token securely in AWS Secrets Manager
- Custom IAM roles for security and reliability
- Infrastructure defined as code (IaC) using CloudFormation
- Easy to recreate if your AWS account is ever terminated

---

## Project Structure

```text
dailygram-auto-poster/
├── README.md
├── deployment/
│   ├── deploy.sh              # Bash deployment script
│   └── deploy.ps1             # Windows deployment script
├── infra/
│   └── template.yaml          # CloudFormation infrastructure
└── lambda/
    └── lambda_post_instagram.py
```

## Prerequisites

- **AWS CLI v2** installed and configured (`aws configure`)
- An **Instagram Business or Creator Account** connected to a Facebook Page
- **Instagram Graph API** access token with `pages_read_engagement`, `pages_manage_posts`, and `instagram_basic` permissions
- **AWS Secrets Manager** secret named `instagram_api_token` containing your access token

---

## Deployment

### One-Command Deploy

Choose the deploy script for your operating system.

From the root of the project, run:

**Linux / macOS:**

```bash
./deployment/deploy.sh
```

**Windows:**

```powershell
./deployment/deploy.ps1
```

This script will:

- Package your Lambda code into `lambda.zip`
- Create (or use) a deployment S3 bucket
- Upload the zip file
- Deploy or update the CloudFormation stack
- Create the S3 bucket, IAM roles, Lambda function, and daily EventBridge Scheduler

The scheduler will run daily at 2:30 PM Eastern Time (America/New_York).

---

## How to Use

1. Upload images/videos and optional captions to the S3 path: `uploads/`
2. The Lambda will automatically pick the oldest file, post it, and move it to `processed/`
3. Monitor logs in CloudWatch Logs under the log group `/aws/lambda/automated-instagram-posting`

> **Tip:** For best results, prepare 30–90 pieces of content in advance and upload them all at once.

---

## Updating the Code
To update your Lambda function:

1. Edit `lambda/lambda_post_instagram.py`
2. Run the deploy script again:

**Linux / macOS:**

```bash
./deployment/deploy.sh
```

**Windows:**

```powershell
./deployment/deploy.ps1
```