#!/bin/bash
set -e

STACK_NAME="dailygram-poster"
REGION="us-east-2"                    # ← Change if your resources are in another region
TEMPLATE_FILE="infra/template.yaml"

echo "=== Packaging Lambda code ==="
cd lambda
zip -r ../lambda.zip . -x "*.DS_Store" "__pycache__/*" "*.pyc" "*/__pycache__/*"
cd ..

echo "=== Creating deployment bucket if needed ==="
aws s3 mb s3://\${DeploymentBucket} --region \${REGION} 2>/dev/null || true

echo "=== Uploading lambda.zip ==="
aws s3 cp lambda.zip s3://\${DeploymentBucket}/lambda.zip

echo "=== Deploying / Updating CloudFormation stack ==="
aws cloudformation deploy \
  --template-file \${TEMPLATE_FILE} \
  --stack-name \${STACK_NAME} \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides BucketName=instagram-posts-bucket \
  --region \${REGION}

echo "=== Cleanup ==="
rm -f lambda.zip

echo "✅ Deployment completed successfully!"
echo "Your Instagram auto-poster will run daily at 2:30 PM Eastern Time."