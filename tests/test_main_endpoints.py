# tests/test_main_endpoints.py
import uuid
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import ServiceError, app
from app.models import (
    Answer,
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
MOCK_TIME = "app.main.time"


@pytest.fixture
def client():
    """テスト用APIクライアント"""
    return TestClient(app)


@pytest.fixture
def dummy_problems():
    """テスト用問題セット"""
    return [create_dummy_problem("Q001"), create_dummy_problem("Q002")]


@pytest.fixture
def more_dummy_problems():
    """より多くのテスト用問題セット"""
    return [create_dummy_problem(f"Q{i:03d}") for i in range(1, 51)]


@pytest.fixture
def fixed_uuid():
    """固定UUID値 (有効な形式に修正)"""
    # 標準的な UUID 形式 (32桁の16進数 + ハイフン)
    return uuid.UUID("12345678-1234-5678-1234-567812345678")


@pytest.fixture
def expected_session_id(fixed_uuid):
    """期待されるセッションID (str()不要)"""
    return f"sess_{fixed_uuid}"


def create_dummy_problem(q_id: str, book_sorce: str = "readable_code") -> ProblemData:
    """テスト用問題データ生成ヘルパー"""
    return ProblemData(
        questionId=q_id,
        bookSource=book_sorce,
        category="test_cat",
        question=f"Question {q_id}",
        options=[Option(id="A", text="A"), Option(id="B", text="B")],
        correctAnswer="A",
        explanation=Explanation(explanation=f"Expl {q_id}"),
    )


@pytest.fixture
def create_session_data():
    """
    カスタマイズ可能なセッションデータを作成するファクトリー関数
    """

    def _create_session_data(correct_answers=None, problems=None, ttl_offset=3600):
        if correct_answers is None and problems is None:
            correct_answers = {"Q001": "A", "Q002": "A"}
        elif correct_answers is not None and problems is None:
            problems = [create_dummy_problem(qid) for qid in correct_answers]
        elif problems is not None:
            if correct_answers is None:
                correct_answers = {p.questionId: p.correctAnswer for p in problems}
        else:
            correct_answers = {p.questionId: p.correctAnswer for p in problems}

        problem_data: Dict[str, SessionDataItem] = {}
        for problem in problems:
            correct_answer = correct_answers.get(
                problem.questionId, problem.correctAnswer
            )
            problem_data[problem.questionId] = SessionDataItem(
                questionId=problem.questionId,
                correctAnswer=correct_answer,
                category=problem.category,
                question=problem.question,
                options=problem.options,
                explanation=problem.explanation.explanation,
            )

        # 固定の未来のタイムスタンプを使用
        current_time = (
            1743889703  # 例: 2025-04-07 13:28:23 JST (これは実行時の現在時刻とは異なる)
        )
        ttl = current_time + ttl_offset
        return SessionData(problem_data=problem_data, ttl=ttl)

    return _create_session_data


# --- Helper Functions for Validation ---


def validate_answer_results(
    data: Dict[str, Any], expected_results: List[Dict[str, Any]]
):
    """
    POST /answers のレスポンス結果を検証するヘルパー関数

    Args:
        data: レスポンスのJSONデータ (辞書)
        expected_results: 期待される結果のリスト。各要素は辞書で
                          questionId, isCorrect, userAnswer, correctAnswer を含む
    """
    assert "results" in data
    assert isinstance(data["results"], list)
    assert len(data["results"]) == len(expected_results)

    # questionId をキーにした辞書に変換して比較しやすくする
    results_dict = {r["questionId"]: r for r in data["results"]}

    for expected in expected_results:
        qid = expected["questionId"]
        assert qid in results_dict, f"Question ID {qid} not found in results"
        result = results_dict[qid]

        assert result["isCorrect"] == expected["isCorrect"], (
            f"Mismatch isCorrect for {qid}"
        )
        assert result["userAnswer"] == expected["userAnswer"], (
            f"Mismatch userAnswer for {qid}"
        )
        assert result["correctAnswer"] == expected["correctAnswer"], (
            f"Mismatch correctAnswer for {qid}"
        )
        # 結果には explanation と options が含まれることも確認
        assert "explanation" in result, f"Missing explanation for {qid}"
        assert isinstance(result["explanation"], str), (
            f"explanation should be str for {qid}"
        )
        assert "options" in result, f"Missing options for {qid}"
        assert isinstance(result["options"], list), f"options should be list for {qid}"


# --- Test GET /questions ---


@pytest.mark.asyncio
async def test_get_questions_success(
    client, dummy_problems, fixed_uuid, expected_session_id
):
    """
    GET /questions エンドポイントの成功ケースをテストします。
    AsyncMock の使用を修正。
    """
    with (
        patch(MOCK_UUID) as mock_uuid,
        # get_questions_from_s3 は async def なので AsyncMock を使用
        patch(MOCK_GET_QUESTIONS_S3, new_callable=AsyncMock) as mock_get_s3,
        # store_session_data は def なので MagicMock (デフォルト) で良い
        patch(MOCK_STORE_SESSION) as mock_store,
    ):
        mock_uuid.uuid4.return_value = fixed_uuid
        mock_get_s3.return_value = dummy_problems  # AsyncMock の return_value

        response = client.get(
            "/questions?bookSource=readable_code&count=2&timeLimit=30"
        )

        assert response.status_code == 200
        data = response.json()

        assert "questions" in data
        assert "timeLimit" in data
        assert "sessionId" in data
        assert data["sessionId"] == expected_session_id
        assert len(data["questions"]) == 2
        assert data["timeLimit"] == 30 * 2

        q1_resp = next(
            (q for q in data["questions"] if q["questionId"] == "Q001"), None
        )
        q2_resp = next(
            (q for q in data["questions"] if q["questionId"] == "Q002"), None
        )
        assert q1_resp is not None
        assert q2_resp is not None
        assert "correctAnswer" not in q1_resp
        assert "explanation" not in q1_resp
        assert "options" in q1_resp

        # AsyncMock の呼び出し検証
        mock_get_s3.assert_awaited_once_with("readable_code", 2)
        # MagicMock の呼び出し検証
        mock_store.assert_called_once()
        call_args = mock_store.call_args[0]
        assert call_args[0] == expected_session_id
        assert len(call_args[1]) == 2
        assert {p.questionId for p in call_args[1]} == {"Q001", "Q002"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "book_source, expected_s3_call",
    [
        ("readable_code", ("readable_code", 2)),
        ("programming_principles", ("programming_principles", 2)),
        ("both", ("both", 2)),
    ],
)
async def test_get_questions_booksource_param(
    client,
    dummy_problems,
    fixed_uuid,
    expected_session_id,
    book_source,
    expected_s3_call,
):
    """GET /questions: bookSource パラメータによる S3 呼び出しの変化をテスト"""
    with (
        patch(MOCK_UUID) as mock_uuid,
        patch(MOCK_GET_QUESTIONS_S3, new_callable=AsyncMock) as mock_get_s3,
        patch(MOCK_STORE_SESSION) as mock_store,
    ):
        mock_uuid.uuid4.return_value = fixed_uuid
        mock_get_s3.return_value = dummy_problems

        response = client.get(
            f"/questions?bookSource={book_source}&count=2&timeLimit=30"
        )

        assert response.status_code == 200
        mock_get_s3.assert_awaited_once_with(*expected_s3_call)
        mock_store.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 50])
async def test_get_questions_count_boundary(
    client, more_dummy_problems, fixed_uuid, expected_session_id, count
):
    """
    GET /questions: count パラメータの境界値をテスト
    """
    with (
        patch(MOCK_UUID) as mock_uuid,
        patch(MOCK_GET_QUESTIONS_S3, new_callable=AsyncMock) as mock_get_s3,
        patch(MOCK_STORE_SESSION) as mock_store,
    ):
        mock_uuid.uuid4.return_value = fixed_uuid
        mock_get_s3.return_value = more_dummy_problems[:count]

        response = client.get(f"/questions?bookSource=both&count={count}&timeLimit=30")

        assert response.status_code == 200
        data = response.json()
        assert len(data["questions"]) == count
        mock_get_s3.assert_awaited_once_with("both", count)
        mock_store.assert_called_once()
        assert len(mock_store.call_args[0][1]) == count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "time_limit_per_q, expected_total_time", [(10, 10 * 2), (300, 300 * 2)]
)
async def test_get_questions_timelimit_boundary(
    client,
    dummy_problems,
    fixed_uuid,
    expected_session_id,
    time_limit_per_q,
    expected_total_time,
):
    """GET /questions: timeLimit パラメータの境界値をテスト"""
    with (
        patch(MOCK_UUID) as mock_uuid,
        patch(MOCK_GET_QUESTIONS_S3, new_callable=AsyncMock) as mock_get_s3,
        patch(MOCK_STORE_SESSION) as mock_store,
    ):
        mock_uuid.uuid4.return_value = fixed_uuid
        mock_get_s3.return_value = dummy_problems

        response = client.get(
            f"/questions?bookSource=both&count=2&timeLimit={time_limit_per_q}"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["timeLimit"] == expected_total_time
        mock_get_s3.assert_awaited_once_with("both", 2)
        mock_store.assert_called_once()


@pytest.mark.asyncio
async def test_get_questions_no_problems_found(client, fixed_uuid):
    """GET /questions: S3から問題が見つからないケース (404)"""
    with (
        patch(MOCK_UUID) as mock_uuid,
        patch(MOCK_GET_QUESTIONS_S3, new_callable=AsyncMock) as mock_get_s3,
        patch(MOCK_STORE_SESSION) as mock_store,
    ):
        mock_uuid.uuid4.return_value = fixed_uuid
        mock_get_s3.return_value = []  # 空リストを返す

        response = client.get(
            "/questions?bookSource=readable_code&count=5&timeLimit=30"
        )

        assert response.status_code == 404
        assert "No questions could be loaded" in response.json()["detail"]
        mock_get_s3.assert_awaited_once_with("readable_code", 5)
        mock_store.assert_not_called()


@pytest.mark.asyncio
async def test_get_questions_s3_error(client):
    """GET /questions: S3からの取得失敗 (500)"""
    with patch(MOCK_GET_QUESTIONS_S3, new_callable=AsyncMock) as mock_get_s3:
        # AsyncMock の side_effect にエラーを設定
        mock_get_s3.side_effect = ServiceError(status_code=500, detail="S3 Mock Error")

        response = client.get(
            "/questions?bookSource=readable_code&count=2&timeLimit=30"
        )

        assert response.status_code == 500
        assert response.json() == {"detail": "S3 Mock Error"}


@pytest.mark.asyncio
async def test_get_questions_db_error(client, fixed_uuid, dummy_problems):
    """GET /questions: DBへの保存失敗 (500)"""
    with (
        patch(MOCK_UUID) as mock_uuid,
        patch(MOCK_GET_QUESTIONS_S3, new_callable=AsyncMock) as mock_get_s3,
        patch(MOCK_STORE_SESSION) as mock_store,
    ):
        mock_uuid.uuid4.return_value = fixed_uuid
        mock_get_s3.return_value = dummy_problems
        # store_session_data (同期関数) のモックにエラーを設定
        mock_store.side_effect = ServiceError(status_code=500, detail="Database error")

        response = client.get(
            "/questions?bookSource=readable_code&count=2&timeLimit=30"
        )

        assert response.status_code == 500
        assert response.json() == {"detail": "Database error"}
        mock_get_s3.assert_awaited_once_with("readable_code", 2)
        mock_store.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "param, value",
    [
        ("count", 0),
        ("count", 51),
        ("timeLimit", 9),
        ("timeLimit", 301),
        ("bookSource", "invalid_source"),
    ],
)
async def test_get_questions_invalid_params(client, param, value):
    """GET /questions: 無効なパラメータ (422)"""
    params = {"bookSource": "readable_code", "count": "2", "timeLimit": "30"}
    params[param] = str(value)
    url = f"/questions?{'&'.join(f'{k}={v}' for k, v in params.items())}"

    response = client.get(url)
    assert response.status_code == 422


# --- Test POST /answers ---


@pytest.mark.asyncio
async def test_post_answers_success(client, create_session_data):
    """POST /answers: 成功ケース (正解/不正解混在)"""
    # get_session_data は同期関数なので MagicMock (デフォルト)
    with patch(MOCK_GET_SESSION) as mock_get_session:
        session_id = "test-session-xyz"
        session_data = create_session_data(correct_answers={"Q001": "A", "Q002": "A"})
        mock_get_session.return_value = session_data

        request_body = {
            "sessionId": session_id,
            "answers": [
                {"questionId": "Q001", "answer": "A", "displayOrder": ["A", "B"]},  # 正
                {"questionId": "Q002", "answer": "B", "displayOrder": ["B", "A"]},  # 誤
            ],
        }
        response = client.post("/answers", json=request_body)

        assert response.status_code == 200
        data = response.json()
        validate_answer_results(
            data,
            [
                {
                    "questionId": "Q001",
                    "isCorrect": True,
                    "userAnswer": "A",
                    "correctAnswer": "A",
                },
                {
                    "questionId": "Q002",
                    "isCorrect": False,
                    "userAnswer": "B",
                    "correctAnswer": "A",
                },
            ],
        )
        mock_get_session.assert_called_once_with(session_id)


@pytest.mark.asyncio
async def test_post_answers_all_correct(client, create_session_data):
    """POST /answers: 全問正解ケース"""
    with patch(MOCK_GET_SESSION) as mock_get_session:
        session_id = "test-session-all-correct"
        session_data = create_session_data({"Q001": "A", "Q002": "B"})
        mock_get_session.return_value = session_data

        request_body = {
            "sessionId": session_id,
            "answers": [
                {"questionId": "Q001", "answer": "A", "displayOrder": ["A", "B"]},
                {"questionId": "Q002", "answer": "B", "displayOrder": ["A", "B"]},
            ],
        }
        response = client.post("/answers", json=request_body)

        assert response.status_code == 200
        data = response.json()
        validate_answer_results(
            data,
            [
                {
                    "questionId": "Q001",
                    "isCorrect": True,
                    "userAnswer": "A",
                    "correctAnswer": "A",
                },
                {
                    "questionId": "Q002",
                    "isCorrect": True,
                    "userAnswer": "B",
                    "correctAnswer": "B",
                },
            ],
        )
        mock_get_session.assert_called_once_with(session_id)


@pytest.mark.asyncio
async def test_post_answers_all_incorrect(client, create_session_data):
    """POST /answers: 全問不正解ケース"""
    with patch(MOCK_GET_SESSION) as mock_get_session:
        session_id = "test-session-all-incorrect"
        session_data = create_session_data({"Q001": "A", "Q002": "A"})
        mock_get_session.return_value = session_data

        request_body = {
            "sessionId": session_id,
            "answers": [
                {"questionId": "Q001", "answer": "B", "displayOrder": ["A", "B"]},
                {"questionId": "Q002", "answer": "B", "displayOrder": ["A", "B"]},
            ],
        }
        response = client.post("/answers", json=request_body)

        assert response.status_code == 200
        data = response.json()
        validate_answer_results(
            data,
            [
                {
                    "questionId": "Q001",
                    "isCorrect": False,
                    "userAnswer": "B",
                    "correctAnswer": "A",
                },
                {
                    "questionId": "Q002",
                    "isCorrect": False,
                    "userAnswer": "B",
                    "correctAnswer": "A",
                },
            ],
        )
        mock_get_session.assert_called_once_with(session_id)


@pytest.mark.asyncio
async def test_post_answers_session_not_found(client):
    """POST /answers: セッションが見つからない (404)"""
    with patch(MOCK_GET_SESSION) as mock_get_session:
        mock_get_session.return_value = None
        session_id = "non-existent-session"
        request_body = {
            "sessionId": session_id,
            "answers": [
                {"questionId": "Q001", "answer": "A", "displayOrder": ["A", "B"]}
            ],
        }
        response = client.post("/answers", json=request_body)

        assert response.status_code == 404
        assert response.json() == {"detail": "Session not found or expired."}
        mock_get_session.assert_called_once_with(session_id)


@pytest.mark.asyncio
async def test_post_answers_session_expired(client, create_session_data):
    """POST /answers: セッション期限切れ (404)"""
    with patch(MOCK_GET_SESSION) as mock_get_session:
        # TTLチェックでNoneが返ることをシミュレート
        mock_get_session.return_value = None
        session_id = "expired-session"
        request_body = {
            "sessionId": session_id,
            "answers": [
                {"questionId": "Q001", "answer": "A", "displayOrder": ["A", "B"]}
            ],
        }
        response = client.post("/answers", json=request_body)

        assert response.status_code == 404
        assert response.json() == {"detail": "Session not found or expired."}
        mock_get_session.assert_called_once_with(session_id)


@pytest.mark.asyncio
async def test_get_session_data_error(client):
    """
    POST /answers: get_session_data がServiceErrorを発生させるケース (500)
    【追加されたテストケース】
    """
    with patch(MOCK_GET_SESSION) as mock_get_session:
        session_id = "test-session-db-error"
        # get_session_data (同期関数) のモックにエラーを設定
        mock_get_session.side_effect = ServiceError(
            status_code=500, detail="Database read error"
        )

        request_body = {
            "sessionId": session_id,
            "answers": [
                {"questionId": "Q001", "answer": "A", "displayOrder": ["A", "B"]}
            ],
        }

        response = client.post("/answers", json=request_body)

        assert response.status_code == 500
        assert response.json() == {"detail": "Database read error"}
        mock_get_session.assert_called_once_with(session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_body",
    [
        {
            "answers": [
                {"questionId": "Q001", "answer": "A", "displayOrder": ["A", "B"]}
            ]
        },
        {"sessionId": "test-session"},
        {
            "sessionId": "test-session",
            "answers": [{"answer": "A", "displayOrder": ["A", "B"]}],
        },
        {
            "sessionId": "test-session",
            "answers": [{"questionId": "Q001", "displayOrder": ["A", "B"]}],
        },
        {
            "sessionId": "test-session",
            "answers": [{"questionId": "Q001", "answer": "A"}],
        },
        {"sessionId": "test-session", "answers": "not_a_list"},
        {"sessionId": "test-session", "answers": ["not_a_dict"]},
    ],
)
async def test_post_answers_validation_error(client, invalid_body):
    """POST /answers: 無効なリクエストボディ (422)"""
    response = client.post("/answers", json=invalid_body)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_answers_empty_answers_list(client, create_session_data):
    """POST /answers: answers リストが空の場合 (200 OK, 空 results)"""
    with patch(MOCK_GET_SESSION) as mock_get_session:
        session_id = "test-session-empty-answers"
        session_data = create_session_data({"Q001": "A"})
        mock_get_session.return_value = session_data

        request_body = {"sessionId": session_id, "answers": []}
        response = client.post("/answers", json=request_body)

        assert response.status_code == 200
        data = response.json()
        assert "results" in data
        assert data["results"] == []
        mock_get_session.assert_called_once_with(session_id)


@pytest.mark.asyncio
async def test_post_answers_duplicate_question_id(client, create_session_data):
    """POST /answers: 重複する questionId がある場合 (両方処理される)"""
    with patch(MOCK_GET_SESSION) as mock_get_session:
        session_id = "test-session-duplicate-qid"
        session_data = create_session_data({"Q001": "A", "Q002": "B"})
        mock_get_session.return_value = session_data

        request_body = {
            "sessionId": session_id,
            "answers": [
                {"questionId": "Q001", "answer": "A", "displayOrder": ["A", "B"]},  # 正
                {"questionId": "Q002", "answer": "B", "displayOrder": ["A", "B"]},  # 正
                {
                    "questionId": "Q001",
                    "answer": "B",
                    "displayOrder": ["A", "B"],
                },  # 誤 (Q001 再度)
            ],
        }
        response = client.post("/answers", json=request_body)

        assert response.status_code == 200
        data = response.json()
        # validate_answer_results は重複IDに対応していないため、手動で検証
        assert "results" in data
        assert len(data["results"]) == 3
        q001_results = [r for r in data["results"] if r["questionId"] == "Q001"]
        q002_results = [r for r in data["results"] if r["questionId"] == "Q002"]
        assert len(q001_results) == 2
        assert len(q002_results) == 1
        assert (
            q001_results[0]["isCorrect"] is True
            and q001_results[0]["userAnswer"] == "A"
        )
        assert (
            q001_results[1]["isCorrect"] is False
            and q001_results[1]["userAnswer"] == "B"
        )
        assert (
            q002_results[0]["isCorrect"] is True
            and q002_results[0]["userAnswer"] == "B"
        )

        mock_get_session.assert_called_once_with(session_id)


@pytest.mark.asyncio
async def test_post_answers_invalid_answer_id(client, create_session_data):
    """POST /answers: 存在しない選択肢IDが指定された場合 (不正解扱い)"""
    with patch(MOCK_GET_SESSION) as mock_get_session:
        session_id = "test-session-invalid-answer"
        session_data = create_session_data({"Q001": "A"})
        mock_get_session.return_value = session_data

        request_body = {
            "sessionId": session_id,
            "answers": [
                {
                    "questionId": "Q001",
                    "answer": "C",
                    "displayOrder": ["A", "B"],
                },  # 不正解
            ],
        }
        response = client.post("/answers", json=request_body)

        assert response.status_code == 200
        data = response.json()
        validate_answer_results(
            data,
            [
                {
                    "questionId": "Q001",
                    "isCorrect": False,
                    "userAnswer": "C",
                    "correctAnswer": "A",
                },
            ],
        )
        mock_get_session.assert_called_once_with(session_id)


@pytest.mark.asyncio
async def test_post_answers_unknown_question_id(client, create_session_data):
    """
    POST /answers: セッションに存在しない questionId を含む解答 (存在するIDのみ結果に)
    【追加されたテストケース】
    """
    with patch(MOCK_GET_SESSION) as mock_get_session:
        session_id = "test-session-unknown-qid"
        session_data = create_session_data({"Q001": "A"})  # Q001 のみ存在
        mock_get_session.return_value = session_data

        request_body = {
            "sessionId": session_id,
            "answers": [
                {
                    "questionId": "Q001",
                    "answer": "A",
                    "displayOrder": ["A", "B"],
                },  # 存在する
                {
                    "questionId": "Q999",
                    "answer": "B",
                    "displayOrder": ["A", "B"],
                },  # 存在しない
            ],
        }
        response = client.post("/answers", json=request_body)

        assert response.status_code == 200
        data = response.json()
        # 存在する Q001 の結果のみ含まれる
        validate_answer_results(
            data,
            [
                {
                    "questionId": "Q001",
                    "isCorrect": True,
                    "userAnswer": "A",
                    "correctAnswer": "A",
                },
            ],
        )
        mock_get_session.assert_called_once_with(session_id)


@pytest.mark.asyncio
async def test_post_answers_session_data_with_empty_problem_data(
    client, create_session_data
):
    """POST /answers: problem_data が空のセッションの場合 (空 results)"""
    with patch(MOCK_GET_SESSION) as mock_get_session:
        session_id = "test-session-empty-problem-data"
        empty_session_data = SessionData(problem_data={}, ttl=9999999999)
        mock_get_session.return_value = empty_session_data

        request_body = {
            "sessionId": session_id,
            "answers": [
                {"questionId": "Q001", "answer": "A", "displayOrder": ["A", "B"]}
            ],
        }
        response = client.post("/answers", json=request_body)

        assert response.status_code == 200
        data = response.json()
        assert "results" in data
        assert data["results"] == []
        mock_get_session.assert_called_once_with(session_id)


@pytest.mark.asyncio
async def test_post_answers_validate_answers_error(client, create_session_data):
    """POST /answers: validate_answers 内部エラー (500)"""
    with (
        patch(MOCK_GET_SESSION) as mock_get_session,
        patch(MOCK_VALIDATE_ANSWERS) as mock_validate,
    ):  # validate_answers (同期) をモック
        session_id = "test-session-validate-error"
        session_data = create_session_data({"Q001": "A"})
        mock_get_session.return_value = session_data
        mock_validate.side_effect = Exception("Unexpected validation error")

        request_body = {
            "sessionId": session_id,
            "answers": [
                {"questionId": "Q001", "answer": "A", "displayOrder": ["A", "B"]}
            ],
        }
        response = client.post("/answers", json=request_body)

        assert response.status_code == 500
        assert (
            response.json()["detail"]
            == "An unexpected error occurred while processing your answers."
        )
        mock_get_session.assert_called_once_with(session_id)
        mock_validate.assert_called_once()
        # 呼び出し引数の検証 (より詳細に)
        call_args, call_kwargs = mock_validate.call_args
        assert len(call_args) == 2
        assert isinstance(call_args[0], list)
        assert len(call_args[0]) == 1
        # Pydantic モデルとして比較
        expected_answer = Answer(questionId="Q001", answer="A", displayOrder=["A", "B"])
        assert call_args[0][0] == expected_answer
        assert call_args[1] == session_data


# --- Test GET / ---


@pytest.mark.asyncio
async def test_root_endpoint(client):
    """GET /: ルートエンドポイントのテスト"""
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"message": "Quiz App Backend is running!"}
