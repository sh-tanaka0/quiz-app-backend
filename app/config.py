# app/config.py
import os

from dotenv import load_dotenv
from pydantic_settings import BaseSettings

load_dotenv()  # .envファイルを読み込む


class Settings(BaseSettings):
    aws_endpoint_url: str | None = os.getenv("AWS_ENDPOINT_URL")  # LocalStack用
    # aws_access_key_id: str = os.getenv("AWS_ACCESS_KEY_ID", "test")
    # aws_secret_access_key: str = os.getenv("AWS_SECRET_ACCESS_KEY", "test")
    aws_default_region: str = os.getenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    dynamodb_table_name: str = os.getenv("DYNAMODB_TABLE_NAME", "QuizSessionTable")
    s3_bucket_name: str = os.getenv("S3_BUCKET_NAME", "quiz-app-bucket")
    frontend_origin: str = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")

    # DynamoDB設定
    dynamodb_quiz_problems_table_name: str = "QuizProblems"
    dynamodb_session_table_name: str = "QuizSessionTable"
    gsi_book_source_index_name: str = "bookSource-questionId-index"


settings = Settings()
