# Dockerfile
# ベースイメージとして公式のPythonイメージを使用
FROM python:3.10-slim

# 作業ディレクトリを設定
WORKDIR /app

# 依存関係ファイルをコピー
COPY requirements.txt .

# 依存関係をインストール
# --no-cache-dir でキャッシュを使用せず、--upgrade pip でpipを最新に
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# アプリケーションコードをコピー
COPY ./app /app/app
COPY .env /app/.env

# アプリケーションがリッスンするポートを指定
EXPOSE 8000

# アプリケーションを起動するコマンド
# --host 0.0.0.0 でコンテナ外部からのアクセスを許可
# --reload は開発時には便利だが、本番環境では外す
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]