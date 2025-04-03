from typing import Dict
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import (
    Explanation,
    Option,
    ProblemData,
    SessionData,
    SessionDataItem,
)

# パス定数
MOCK_GET_QUESTIONS_S3 = "app.main.get_questions_from_s3"
MOCK_STORE_SESSION = "app.main.store_session_data"
MOCK_GET_SESSION = "app.main.get_session_data"
MOCK_VALIDATE_ANSWERS = "app.main.validate_answers"
MOCK_UUID = "app.main.uuid"


@pytest.fixture
def client():
    """テスト用APIクライアント"""
    return TestClient(app)


@pytest.fixture
def dummy_problems():
    """テスト用問題セット"""
    return [create_dummy_problem("Q001"), create_dummy_problem("Q002")]


@pytest.fixture
def fixed_uuid():
    """固定UUID値"""
    return "fixed-test-uuid-1234"


@pytest.fixture
def expected_session_id(fixed_uuid):
    """期待されるセッションID"""
    return f"sess_{fixed_uuid}"


def create_dummy_problem(q_id: str) -> ProblemData:
    """テスト用問題データ生成ヘルパー"""
    return ProblemData(
        questionId=q_id,
        bookSource="test_source",
        category="test_cat",
        question=f"Question {q_id}",
        options=[Option(id="A", text="A"), Option(id="B", text="B")],
        correctAnswer="A",
        explanation=f"Expl {q_id}",
    )


@pytest.fixture
def create_session_data():
    """
    カスタマイズ可能なセッションデータを作成するファクトリー関数
    問題ごとに正解を設定できる
    """

    def _create_session_data(correct_answers=None):
        # デフォルト値
        if correct_answers is None:
            correct_answers = {"Q001": "A", "Q002": "A"}

        problem_data: Dict[str, SessionDataItem] = {}
        for q_id, answer in correct_answers.items():
            problem_data[q_id] = SessionDataItem(
                questionId=q_id,
                correctAnswer=answer,
                category="test_cat",
                question=f"Question {q_id}",
                options=[Option(id="A", text="A"), Option(id="B", text="B")],
                explanation=Explanation(explanation=f"Expl {q_id}"),
            )

        return SessionData(problem_data=problem_data, ttl=9999999999)

    return _create_session_data


@pytest.mark.asyncio
async def test_get_questions_success(
    client, dummy_problems, fixed_uuid, expected_session_id
):
    """
    GET /questions エンドポイントの成功ケースをテストします。
    このテストでは以下を検証します:
    1. エンドポイントが成功時にステータスコード200を返すこと。
    2. レスポンスが期待される構造を持つこと ("questions", "timeLimit", "sessionId" を含む)。
    3. レスポンス内のセッションIDが期待される固定UUIDと一致すること。
    4. レスポンス内の問題数がリクエストされた数と一致すること。
    5. レスポンスに正解情報 (correctAnswer) が含まれないこと。
    6. モックされた依存関数が期待される引数で呼び出されること。
    7. 問題IDのセットが一致すること。
    8. 問題の内容がシャッフルされていても維持されていること。

    """
    # モックのセットアップ
    with (
        patch(MOCK_UUID) as mock_uuid,
        patch(MOCK_GET_QUESTIONS_S3) as mock_get_s3,
        patch(MOCK_STORE_SESSION) as mock_store,
    ):
        # UUID モック
        mock_uuid.uuid4.return_value = fixed_uuid

        # 非同期関数の適切なモックセットアップ
        async def mock_get_questions(*args, **kwargs):
            return dummy_problems

        mock_get_s3.side_effect = mock_get_questions

        # API呼び出し
        response = client.get(
            "/questions?bookSource=readable_code&count=2&timeLimit=30"
        )

        # アサーション
        assert response.status_code == 200
        data = response.json()

        # レスポンスの構造を検証
        assert "questions" in data
        assert "timeLimit" in data
        assert "sessionId" in data
        assert data["sessionId"] == expected_session_id

        # 問題データの検証
        assert len(data["questions"]) == 2
        assert data["questions"][0]["questionId"] == "Q001"
        assert data["questions"][1]["questionId"] == "Q002"
        assert "correctAnswer" not in data["questions"][0]

        # モック呼び出しの検証 - より詳細に
        mock_get_s3.assert_called_once_with("readable_code", 2)
        mock_store.assert_called_once()
        call_args = mock_store.call_args[0]
        assert call_args[0] == expected_session_id
        assert len(call_args[1]) == 2

        # 問題IDのセットが一致することを確認
        assert {p.questionId for p in call_args[1]} == {
            p.questionId for p in dummy_problems
        }

        # シャッフルされていても各問題の内容が維持されていることを確認
        stored_problems_dict = {p.questionId: p for p in call_args[1]}
        for original_problem in dummy_problems:
            stored_problem = stored_problems_dict[original_problem.questionId]
            assert stored_problem.question == original_problem.question
            assert stored_problem.correctAnswer == original_problem.correctAnswer


@pytest.mark.asyncio
async def test_get_questions_s3_error(client):
    """
    GET /questions でS3からの取得に失敗するケースをテストします。
    例外が発生した場合、500エラーを返すことを確認します。
    例外メッセージは "S3 Mock Error" であることを確認します。

    """
    # モックのセットアップ
    with patch(MOCK_GET_QUESTIONS_S3) as mock_get_s3:
        from app.main import ServiceError

        # 例外を発生させる非同期モック
        async def mock_error(*args, **kwargs):
            raise ServiceError(status_code=500, detail="S3 Mock Error")

        mock_get_s3.side_effect = mock_error

        # API呼び出し
        response = client.get(
            "/questions?bookSource=readable_code&count=2&timeLimit=30"
        )

        # アサーション
        assert response.status_code == 500
        assert response.json() == {"detail": "S3 Mock Error"}


@pytest.mark.asyncio
async def test_get_questions_db_error(client, fixed_uuid, dummy_problems):
    """
    GET /questions でDBへの保存に失敗するケースをテストします。
    例外が発生した場合、500エラーを返すことを確認します。
    例外メッセージは "Database error" であることを確認します。

    """
    # モックのセットアップ
    with (
        patch(MOCK_UUID) as mock_uuid,
        patch(MOCK_GET_QUESTIONS_S3) as mock_get_s3,
        patch(MOCK_STORE_SESSION) as mock_store,
    ):
        # UUID モック
        mock_uuid.uuid4.return_value = fixed_uuid

        # S3からの問題取得は成功
        async def mock_get_questions(*args, **kwargs):
            return dummy_problems

        mock_get_s3.side_effect = mock_get_questions

        # DBへの保存が失敗
        from app.main import ServiceError

        async def mock_db_error(*args, **kwargs):
            raise ServiceError(status_code=500, detail="Database error")

        mock_store.side_effect = mock_db_error

        # API呼び出し
        response = client.get(
            "/questions?bookSource=readable_code&count=2&timeLimit=30"
        )

        # アサーション
        assert response.status_code == 500
        assert response.json() == {"detail": "Database error"}


@pytest.mark.asyncio
async def test_post_answers_success(client, create_session_data):
    """
    POST /answers エンドポイントの成功ケースをテストします。
    このテストでは以下を検証します:
    1. エンドポイントが成功時にステータスコード200を返すこと。
    2. レスポンスが期待される構造を持つこと ("results" を含む)。
    3. レスポンス内の結果が正しい形式を持つこと。
    4. ユーザーの解答が正解と一致する場合、`isCorrect` 属性が `True` であること。
    5. ユーザーの解答が不正解の場合、`isCorrect` 属性が `False` であること。
    6. モックされた依存関数が期待される引数で呼び出されること。
    7. セッションデータが正しく取得されていること。
    
    """
    # get_session_dataのみモック
    with patch(MOCK_GET_SESSION) as mock_get_session:
        session_id = "test-session-xyz"

        # Q001は正解「A」に対して「A」を送信、Q002は正解「A」に対して「B」を送信するパターン
        session_data = create_session_data({"Q001": "A", "Q002": "A"})
        mock_get_session.return_value = session_data

        # リクエスト準備
        request_body = {
            "sessionId": session_id,
            "answers": [
                {"questionId": "Q001", "answer": "A", "displayOrder": ["A", "B"]},
                {"questionId": "Q002", "answer": "B", "displayOrder": ["B", "A"]},
            ],
        }

        # API呼び出し
        response = client.post("/answers", json=request_body)

        # アサーション
        assert response.status_code == 200
        data = response.json()
        assert "results" in data
        assert len(data["results"]) == 2

        # 結果の検証
        assert data["results"][0]["isCorrect"] is True  # Q001は正解
        assert data["results"][0]["userAnswer"] == "A"
        assert data["results"][0]["correctAnswer"] == "A"

        assert data["results"][1]["isCorrect"] is False  # Q002は不正解
        assert data["results"][1]["userAnswer"] == "B"
        assert data["results"][1]["correctAnswer"] == "A"

        # モック呼び出しの検証
        mock_get_session.assert_called_once_with(session_id)


@pytest.mark.asyncio
async def test_post_answers_all_correct(client, create_session_data):
    """
    POST /answers エンドポイントですべての回答が正解の場合をテストします。
    """
    with patch(MOCK_GET_SESSION) as mock_get_session:
        session_id = "test-session-xyz"

        # 両方とも正解「A」に対して「A」を送信するパターン
        session_data = create_session_data({"Q001": "A", "Q002": "A"})
        mock_get_session.return_value = session_data

        # リクエスト準備
        request_body = {
            "sessionId": session_id,
            "answers": [
                {"questionId": "Q001", "answer": "A", "displayOrder": ["A", "B"]},
                {"questionId": "Q002", "answer": "A", "displayOrder": ["A", "B"]},
            ],
        }

        # API呼び出し
        response = client.post("/answers", json=request_body)

        # アサーション
        assert response.status_code == 200
        data = response.json()

        # すべての結果が正解であることを確認
        assert all(result["isCorrect"] for result in data["results"])


@pytest.mark.asyncio
async def test_post_answers_all_incorrect(client, create_session_data):
    """
    POST /answers エンドポイントですべての回答が不正解の場合をテストします。
    """
    with patch(MOCK_GET_SESSION) as mock_get_session:
        session_id = "test-session-xyz"

        # 両方とも正解「A」に対して「B」を送信するパターン
        session_data = create_session_data({"Q001": "A", "Q002": "A"})
        mock_get_session.return_value = session_data

        # リクエスト準備
        request_body = {
            "sessionId": session_id,
            "answers": [
                {"questionId": "Q001", "answer": "B", "displayOrder": ["A", "B"]},
                {"questionId": "Q002", "answer": "B", "displayOrder": ["A", "B"]},
            ],
        }

        # API呼び出し
        response = client.post("/answers", json=request_body)

        # アサーション
        assert response.status_code == 200
        data = response.json()

        # すべての結果が不正解であることを確認
        assert not any(result["isCorrect"] for result in data["results"])


@pytest.mark.asyncio
async def test_post_answers_session_not_found(client):
    """
    POST /answers エンドポイントでセッションが見つからないケースをテストします。
    """
    with patch(MOCK_GET_SESSION) as mock_get_session:
        mock_get_session.return_value = None

        session_id = "not-found-session"
        request_body = {"sessionId": session_id, "answers": []}
        response = client.post("/answers", json=request_body)

        assert response.status_code == 404
        assert response.json() == {"detail": "Session not found or expired."}
        mock_get_session.assert_called_once_with(session_id)


@pytest.mark.asyncio
async def test_get_questions_invalid_params(client):
    """
    GET /questions に無効なパラメータを渡した場合のテストです。
    """
    # count が範囲外のケース
    response = client.get("/questions?bookSource=readable_code&count=0&timeLimit=30")
    assert response.status_code == 422

    # timeLimit が範囲外のケース
    response = client.get("/questions?bookSource=readable_code&count=2&timeLimit=5")
    assert response.status_code == 422

    # bookSource が無効なケース
    response = client.get("/questions?bookSource=invalid_source&count=2&timeLimit=30")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_answers_validation_error(client):
    """
    POST /answers に無効なリクエストボディを渡した場合のテストです。
    """
    # sessionId がない場合
    request_body = {
        "answers": [{"questionId": "Q001", "answer": "A", "displayOrder": ["A", "B"]}]
    }
    response = client.post("/answers", json=request_body)
    assert response.status_code == 422

    # answers がない場合
    request_body = {"sessionId": "test-session"}
    response = client.post("/answers", json=request_body)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_root_endpoint(client):
    """
    ルートエンドポイント (/) のテストです。
    """
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"message": "Quiz App Backend is running!"}
