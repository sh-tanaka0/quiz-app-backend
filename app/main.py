# app/main.py
from fastapi import FastAPI, HTTPException, Depends, Query, Body
from typing import List, Literal, Annotated
import random
import uuid
import json
import time
from botocore.exceptions import ClientError

from .models import ( # 次のステップで定義
    QuestionRequestParams,
    QuestionResponse,
    Question,
    Option,
    AnswerRequest,
    Answer,
    AnswerResponse,
    Result,
    ProblemData, # S3のデータ構造モデル
    SessionData # DynamoDBのデータ構造（簡易版）
)
from .aws_clients import s3_client, dynamodb_table
from .config import settings

app = FastAPI(title="Quiz App Backend")

# --- エラーハンドリング (例) ---
class ServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail

@app.exception_handler(ServiceError)
async def service_exception_handler(request, exc: ServiceError):
    return HTTPException(status_code=exc.status_code, detail=exc.detail)

# --- ヘルパー関数など (次のステップで詳細実装) ---
async def get_questions_from_s3(book_source: Literal["readable_code", "programming_principles", "both"], count: int) -> List[ProblemData]:
    # ここにS3から問題データを取得・パースするロジックを実装
    pass

def shuffle_options(questions: List[ProblemData]) -> List[ProblemData]:
    # 選択肢をシャッフルするロジック
    for q in questions:
        random.shuffle(q.options)
    return questions

def store_session_data(session_id: str, problems: List[ProblemData]):
    # DynamoDBにセッションデータを保存するロジック
    pass

def get_session_data(session_id: str) -> dict | None:
     # DynamoDBからセッションデータを取得するロジック
     pass

def validate_answers(user_answers: List[Answer], correct_data: dict) -> List[Result]:
    # 解答を検証・採点するロジック
    pass


@app.get("/questions", response_model=QuestionResponse)
async def get_quiz_questions(
    bookSource: Annotated[Literal["readable_code", "programming_principles", "both"], Query(description="問題の出典")],
    count: Annotated[int, Query(ge=1, le=50, description="取得する問題数")],
    timeLimit: Annotated[int, Query(ge=10, le=300, description="1問あたりの制限時間(秒)")]
):
    # ここに /questions のロジックを実装
    # 1. S3から問題取得 (get_questions_from_s3)
    # 2. 選択肢シャッフル (shuffle_options)
    # 3. セッションID生成
    # 4. DynamoDBにセッション情報保存 (store_session_data)
    # 5. レスポンス整形
    # (ステップ9で詳細化)
    raise NotImplementedError("Endpoint /questions not implemented yet.")


@app.post("/answers", response_model=AnswerResponse)
async def submit_answers(
    answer_request: Annotated[AnswerRequest, Body(description="ユーザーの解答")]
):
    # ここに /answers のロジックを実装
    # 1. DynamoDBからセッションデータ取得 (get_session_data)
    # 2. セッション検証
    # 3. 解答検証・採点 (validate_answers)
    # 4. レスポンス生成
    raise NotImplementedError("Endpoint /answers not implemented yet.")


@app.get("/")
async def root():
    return {"message": "Quiz App Backend is running!"}

# --- /app ディレクトリ内に __init__.py を作成 ---
# touch app/__init__.py