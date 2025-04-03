#!/bin/bash
echo "Waiting for LocalStack to be ready..."
# 簡単なヘルスチェック (より堅牢なチェックも可能)
until curl -s http://localhost:4566/_localstack/health | grep '"s3": "running"' > /dev/null && \
      curl -s http://localhost:4566/_localstack/health | grep '"dynamodb": "running"' > /dev/null; do
  echo -n "."
  sleep 1
done
echo "LocalStack is ready. Initializing resources..."

# 環境変数から値を取得 (docker-compose.yml と合わせる)
REGION="ap-northeast-1"
TABLE_NAME="QuizSessionTable"
BUCKET_NAME="quiz-app-bucket"
ENDPOINT_URL="http://localhost:4566" # スクリプト実行環境から見たLocalStack

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
# 事前にサンプル問題JSONファイルを用意しておく
mkdir -p sample_data/questions/readable_code
mkdir -p sample_data/questions/programming_principles

# readable_code 用サンプル (RC001.json)
cat <<EOF > sample_data/questions/readable_code/RC001.json
{
  "questionId": "RC001",
  "bookSource": "readable_code",
  "category": "readability",
  "question": "コード中に適切なコメントがあると可読性が向上します。\n適切なコメントをつける上で、何をコメントすべきかを考える必要がありますが、次のうち当てはまらないものはどれですか。(1つ選択してください)",
  "options": [
    {"id": "A", "text": "コードからすぐに分かることをコメントに書かない。"},
    {"id": "B", "text": "酷いコードに優れたコメントを書く前に、酷いコードを優れたコードに修正できないかを考える。"},
    {"id": "C", "text": "コメントは多ければ多いほど読み手にとっていいので、全ての行に対してコメントを書く。"},
    {"id": "D", "text": "コメントは「何をやっているか」を書くのでなく、「何故そうなっているのか」を書いた方がよりいい。"}
  ],
  "correctAnswer": "C",
  "explanation": {
    "explanation": "コメントはコードの意図や理由を補足するものであり、コードからすぐに分かることや冗長なコメントは避けるべきです(A)。酷いコードにコメントを追加するよりも、まずコード自体を改善することが重要です(B)。また、コメントは動作そのものよりも、その背景や理由を説明する方が役立ちます(D)。しかし、コメントは多ければ多いほど良いという考えは誤りであり、適切な量と内容が重要です。全ての行にコメントをつけると可読性が低下し、重要な情報が埋もれてしまいます。",
    "referencePages": "49-52",
    "additionalResources": [
      {"type": "article", "title": "読みやすいコメントを書くためのガイドライン", "url": "https://example.com/writing-readable-comments"}
    ]
  }
}
EOF

# programming_principles 用サンプル (PP001.json)
cat <<EOF > sample_data/questions/programming_principles/PP001.json
{
  "questionId": "PP001",
  "bookSource": "programming_principles",
  "category": "principles",
  "question": "ソフトウェア設計の原則として知られる「単一責任の原則（SRP）」について、最もよく説明しているものはどれですか？",
  "options": [
    {"id": "A", "text": "ソフトウェアのエンティティ（クラス、モジュール、関数など）は、変更に対しては閉じており、拡張に対しては開いているべきである。"},
    {"id": "B", "text": "クラスを変更する理由は一つだけであるべきである。"},
    {"id": "C", "text": "派生型はその基本型と置換可能でなければならない。"},
    {"id": "D", "text": "具体的な実装ではなく、抽象に依存すべきである。"}
  ],
  "correctAnswer": "B",
  "explanation": {
    "explanation": "単一責任の原則（SRP）は、クラスやモジュールが担当すべき責任はただ一つであるべきだという原則です。これにより、変更の影響範囲が限定され、コードの保守性や理解しやすさが向上します。他の選択肢は、A: オープン/クローズド原則、C: リスコフの置換原則、D: 依存関係逆転の原則 を説明しています。",
    "referencePages": "85-88",
    "additionalResources": []
  }
}
EOF

# S3にアップロード
aws --endpoint-url=${ENDPOINT_URL} s3 cp sample_data/questions/ s3://${BUCKET_NAME}/questions/ --recursive --region ${REGION}

echo "Sample data uploaded."
rm -rf sample_data # 一時ファイルを削除