# app/models.py
import uuid
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field

# --- API関連モデル ---


class QuestionRequestParams(BaseModel):
    """GET /questions のリクエストパラメータモデル (FastAPIがQueryから自動生成するが、明示も可能)"""

    bookSource: Literal["readable_code", "programming_principles", "both"]
    count: int = Field(..., ge=1, le=50)
    timeLimit: int = Field(..., ge=10, le=300)


class Option(BaseModel):
    """選択肢"""

    id: str
    text: str


class Question(BaseModel):
    """APIレスポンス用の問題形式"""

    questionId: str
    question: str
    options: List[Option]


class QuestionResponse(BaseModel):
    """GET /questions のレスポンスモデル"""

    questions: List[Question]
    timeLimit: int  # 仕様書では "timeLimit" だが合計時間かもしれない totalTime?
    sessionId: str = Field(default_factory=lambda: f"sess_{uuid.uuid4()}")


class Answer(BaseModel):
    """ユーザーの解答"""

    questionId: str
    answer: str  # 選択肢ID (例: "A")


class AnswerRequest(BaseModel):
    """POST /answers のリクエストボディモデル"""

    sessionId: str
    answers: List[Answer]


class Result(BaseModel):
    """採点結果"""

    questionId: str
    category: str | None = None  # S3データから取得
    isCorrect: bool
    userAnswer: str
    correctAnswer: str
    question: str  # S3データから取得
    options: List[Option]  # S3データから取得 (ユーザーが見た表示順とは限らない)
    explanation: str | None = None  # S3データから取得


class AnswerResponse(BaseModel):
    """POST /answers のレスポンスモデル"""

    results: List[Result]


# --- データ構造モデル ---


class Explanation(BaseModel):
    explanation: str
    referencePages: str | None = None
    additionalResources: List[Dict[str, Any]] | None = []


class ProblemData(BaseModel):
    """S3に保存される問題データの構造"""

    questionId: str
    bookSource: str
    category: str | None = None
    question: str
    options: List[Option]
    correctAnswer: str
    explanation: Explanation


class SessionDataItem(BaseModel):
    """DynamoDBに保存する各問題の情報 (簡易版)"""

    questionId: str
    correctAnswer: str
    category: str | None = None
    question: str  # 結果表示用に保存
    options: List[Option]  # 結果表示用に保存
    explanation: str | None = None  # 結果表示用に保存


class SessionData(BaseModel):
    """DynamoDBに保存するセッション全体のデータ (sessionIdはキーなので含めない)"""

    problem_data: Dict[str, SessionDataItem]  # 問題IDをキーにした辞書
    ttl: int  # TTLタイムスタンプ
