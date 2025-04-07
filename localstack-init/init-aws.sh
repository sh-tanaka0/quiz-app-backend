#!/bin/bash

# 環境変数から値を取得 (docker-compose.yml と合わせる)
REGION="ap-northeast-1"
TABLE_NAME="QuizSessionTable"
BUCKET_NAME="quiz-app-bucket"
ENDPOINT_URL="http://localhost:4566" # スクリプト実行環境から見たLocalStack
SAMPLE_DATA_DIR="./sample_data" # Python スクリプトで生成したデータがある場所


echo "Creating S3 bucket: ${BUCKET_NAME}"
aws --endpoint-url=${ENDPOINT_URL} s3api create-bucket --bucket ${BUCKET_NAME} --region ${REGION} --create-bucket-configuration LocationConstraint=${REGION}

echo "Creating DynamoDB table: ${TABLE_NAME}"
aws --endpoint-url=${ENDPOINT_URL} dynamodb create-table \
    --table-name ${TABLE_NAME} \
    --attribute-definitions \
        AttributeName=sessionId,AttributeType=S \
    --key-schema \
        AttributeName=sessionId,KeyType=HASH \
    --provisioned-throughput \
        ReadCapacityUnits=5,WriteCapacityUnits=5 \
    --region ${REGION}

# TTL設定を有効化
echo "Enabling TTL for DynamoDB table: ${TABLE_NAME}"
aws --endpoint-url=${ENDPOINT_URL} dynamodb update-time-to-live \
    --table-name ${TABLE_NAME} \
    --time-to-live-specification "Enabled=true, AttributeName=ttl" \
    --region ${REGION}

echo "AWS resources initialized."

# --- オプション: サンプル問題データのアップロード ---
echo "Uploading sample questions to S3 bucket: ${BUCKET_NAME}"
# --- サンプル問題データのアップロード (修正箇所) ---
# 事前に python generate_samples.py で生成されたデータがあるか確認
if [ -d "${SAMPLE_DATA_DIR}/questions" ]; then
  echo "Uploading sample questions from ${SAMPLE_DATA_DIR}/questions to S3..."
  # aws s3 sync を使って sample_data/questions ディレクトリの内容を S3 バケットの /questions/ プレフィックスに同期（アップロード）
  aws --endpoint-url=${ENDPOINT_URL} s3 sync \
      "${SAMPLE_DATA_DIR}/questions" \
      "s3://${BUCKET_NAME}/questions/" \
      --region ${REGION} || echo "WARN: Failed to upload sample data from ${SAMPLE_DATA_DIR}/questions."
  echo "Sample data upload attempt finished."
else
  echo "WARN: Sample data directory not found: ${SAMPLE_DATA_DIR}/questions. Skipping sample data upload."
  echo "INFO: Run 'python generate_samples.py' first to create sample data."
fi

echo "Sample data upload attempt finished."
rm -rf sample_data # 一時ファイルを削除