# tests/test_main_utils.py
from decimal import Decimal  # DynamoDB の数値型 Decimal 対策
from typing import List
from unittest.mock import patch 

import pytest

# テスト対象の関数を main からインポート (パスは環境に合わせる)
from app.main import (
    SESSION_TTL_SECONDS,  # TTL 値
    ServiceError,  # 必要なら
    get_session_data,
    shuffle_options,
    store_session_data,
)

# 必要なモデルをインポート (パスは環境に合わせる)
from app.models import (
    Explanation,
    Option,
    ProblemData,
    SessionData,
    SessionDataItem,
)

# --- Fixtures ---


# test_validation.py と共通の fixture を使いたい場合は、
# conftest.py に定義することを検討する。ここでは再定義する。
def create_test_problem(
    q_id: str, book_src: str = "test_source", num_options: int = 3
) -> ProblemData:
    """テスト用の ProblemData を生成するヘルパー"""
    options = [
        Option(id=chr(65 + i), text=f"Option {chr(65 + i)}") for i in range(num_options)
    ]
    return ProblemData(
        questionId=q_id,
        bookSource=book_src,
        category=f"Cat {q_id}",
        question=f"Question text for {q_id}",
        options=options,
        correctAnswer="A",  # 仮に A を正解とする
        explanation=Explanation(explanation=f"Explanation for {q_id}"),
    )


@pytest.fixture
def problem_list_fixture():
    """複数の ProblemData を含むリストを返す Fixture"""
    return [
        create_test_problem("Q001", num_options=4),
        create_test_problem("Q002", num_options=3),
        create_test_problem("Q003", num_options=1),  # 選択肢が1つ
        create_test_problem("Q004", num_options=0),  # 選択肢が0
    ]


# --- Tests for shuffle_options ---


def test_shuffle_options_basic(problem_list_fixture):
    """shuffle_options: 基本的なシャッフル動作（要素数、ID構成の維持）を確認"""
    problems = [
        p.model_copy(deep=True) for p in problem_list_fixture[:2]
    ]  # Q001, Q002 をコピーして使用
    original_options_q1 = problems[0].options[:]  # 元の順序をコピー
    original_options_q2 = problems[1].options[:]

    # 関数を実行 (元のリストを変更する副作用がある)
    shuffled_problems = shuffle_options(problems)

    # 返り値が元のリストと同じオブジェクトであること (in-place shuffle)
    assert shuffled_problems is problems

    # 問題数は変わらない
    assert len(shuffled_problems) == 2

    # 各問題の選択肢の要素数が変わらないこと
    assert len(shuffled_problems[0].options) == len(original_options_q1)
    assert len(shuffled_problems[1].options) == len(original_options_q2)

    # 各問題の選択肢の ID 構成が変わらないこと (セットで比較)
    assert {opt.id for opt in shuffled_problems[0].options} == {
        opt.id for opt in original_options_q1
    }
    assert {opt.id for opt in shuffled_problems[1].options} == {
        opt.id for opt in original_options_q2
    }

    # 選択肢の中身 (text) が維持されていることを確認 (代表例)
    assert any(opt.text == "Option A" for opt in shuffled_problems[0].options)
    assert any(opt.text == "Option B" for opt in shuffled_problems[1].options)


def test_shuffle_options_single_option(problem_list_fixture):
    """shuffle_options: 選択肢が1つの場合は順序は変わらない"""
    problems = [problem_list_fixture[2].model_copy(deep=True)]  # Q003 (選択肢1つ)
    original_options = problems[0].options[:]

    shuffled_problems = shuffle_options(problems)

    assert shuffled_problems is problems
    assert len(shuffled_problems[0].options) == 1
    # 順序も変わらないはず
    assert shuffled_problems[0].options[0].id == original_options[0].id


def test_shuffle_options_no_options(problem_list_fixture):
    """shuffle_options: 選択肢が0個の場合でもエラーにならない"""
    problems = [problem_list_fixture[3].model_copy(deep=True)]  # Q004 (選択肢0個)

    shuffled_problems = shuffle_options(problems)

    assert shuffled_problems is problems
    assert len(shuffled_problems[0].options) == 0


def test_shuffle_options_empty_list():
    """shuffle_options: 問題リストが空の場合でもエラーにならない"""
    problems: List[ProblemData] = []
    shuffled_problems = shuffle_options(problems)
    assert shuffled_problems == []


# --- Tests for store_session_data ---

# DynamoDB テーブルのモック用パス (環境に合わせて修正)
MOCK_DYNAMODB_TABLE = "app.main.dynamodb_table"
MOCK_TIME_MODULE = "app.main.time"  # time モジュール全体


@patch(MOCK_DYNAMODB_TABLE)  # dynamodb_table をモック
@patch(MOCK_TIME_MODULE)  # time モジュールをモック
def test_store_session_data_success(mock_time, mock_db_table, problem_list_fixture):
    """store_session_data: 正常にデータが保存されること"""
    mock_time.time.return_value = 1700000000  # 固定の現在時刻
    session_id = "test-session-store-1"
    problems = problem_list_fixture[:2]  # Q001, Q002

    store_session_data(session_id, problems)

    # time.time が呼ばれたか確認
    mock_time.time.assert_called_once()

    # dynamodb_table.put_item が呼ばれたか確認
    mock_db_table.put_item.assert_called_once()

    # put_item に渡された引数 (Item) を確認
    call_args, call_kwargs = mock_db_table.put_item.call_args
    assert "Item" in call_kwargs
    item = call_kwargs["Item"]

    # sessionId が正しいか
    assert item["sessionId"] == session_id

    # TTL が正しく計算されているか (固定時刻 + SESSION_TTL_SECONDS)
    expected_ttl = 1700000000 + SESSION_TTL_SECONDS
    assert item["ttl"] == expected_ttl

    # problem_data が正しく変換されているか
    assert "problem_data" in item
    assert isinstance(item["problem_data"], dict)
    assert len(item["problem_data"]) == 2  # 問題数

    # Q001 のデータを確認 (部分的に)
    assert "Q001" in item["problem_data"]
    q1_data = item["problem_data"]["Q001"]
    assert q1_data["questionId"] == "Q001"
    assert q1_data["correctAnswer"] == "A"  # create_test_problem のデフォルト
    assert q1_data["question"] == "Question text for Q001"
    assert q1_data["explanation"] == "Explanation for Q001"
    assert isinstance(q1_data["options"], list)
    assert len(q1_data["options"]) == 4  # Q001 は選択肢4つで作成
    assert q1_data["options"][0] == {
        "id": "A",
        "text": "Option A",
    }  # Pydantic -> dict 変換後


@patch(MOCK_DYNAMODB_TABLE)
@patch(MOCK_TIME_MODULE)
def test_store_session_data_db_error(mock_time, mock_db_table, problem_list_fixture):
    """store_session_data: DynamoDB でエラーが発生した場合に ServiceError"""
    mock_time.time.return_value = 1700000000
    # put_item が ClientError を送出するように設定
    from botocore.exceptions import ClientError

    mock_db_table.put_item.side_effect = ClientError(
        error_response={
            "Error": {
                "Code": "ProvisionedThroughputExceededException",
                "Message": "Limit exceeded",
            }
        },
        operation_name="PutItem",
    )

    session_id = "test-session-store-error"
    problems = problem_list_fixture[:1]

    with pytest.raises(ServiceError) as excinfo:
        store_session_data(session_id, problems)

    assert excinfo.value.status_code == 500
    assert "Failed to store session data" in excinfo.value.detail
    mock_db_table.put_item.assert_called_once()  # エラーでも呼び出しはされる


# --- Tests for get_session_data ---


@patch(MOCK_DYNAMODB_TABLE)
@patch(MOCK_TIME_MODULE)
def test_get_session_data_success(mock_time, mock_db_table):
    """get_session_data: 正常にデータが取得・パースされること"""
    session_id = "test-session-get-1"
    current_time = 1700000000
    ttl_valid = current_time + 1000  # 有効なTTL
    mock_time.time.return_value = current_time

    # get_item が返すダミーの DynamoDB アイテム
    # DynamoDB は数値を Decimal で返すことがあるため、それを模倣
    mock_item = {
        "sessionId": session_id,
        "ttl": Decimal(str(ttl_valid)),  # Decimal で返す
        "problem_data": {
            "Q101": {
                "questionId": "Q101",
                "correctAnswer": "C",
                "category": "Get Test",
                "question": "Get Q1",
                "options": [{"id": "A", "text": "A"}, {"id": "C", "text": "C"}],
                "explanation": "Get E1",
            }
        },
    }
    mock_db_table.get_item.return_value = {"Item": mock_item}

    session_data = get_session_data(session_id)

    # time.time が呼ばれたか (TTL チェックのため)
    mock_time.time.assert_called_once()
    # get_item が正しいキーで呼ばれたか
    mock_db_table.get_item.assert_called_once_with(Key={"sessionId": session_id})

    # SessionData オブジェクトが返されること
    assert isinstance(session_data, SessionData)
    # TTL が int に変換されていること
    assert session_data.ttl == ttl_valid
    # problem_data が正しくパースされていること
    assert "Q101" in session_data.problem_data
    item_data = session_data.problem_data["Q101"]
    assert isinstance(item_data, SessionDataItem)
    assert item_data.correctAnswer == "C"
    assert isinstance(item_data.options, list)
    assert len(item_data.options) == 2
    assert isinstance(
        item_data.options[0], Option
    )  # Option オブジェクトにパースされている
    assert item_data.options[0].id == "A"


@patch(MOCK_DYNAMODB_TABLE)
@patch(MOCK_TIME_MODULE)
def test_get_session_data_not_found(mock_time, mock_db_table):
    """get_session_data: セッションデータが見つからない場合に None"""
    session_id = "test-session-get-notfound"
    # get_item が空のレスポンス (Item がない) を返す
    mock_db_table.get_item.return_value = {}

    session_data = get_session_data(session_id)

    assert session_data is None
    mock_db_table.get_item.assert_called_once_with(Key={"sessionId": session_id})
    # Item がなければ TTL チェックは行われないので time.time は呼ばれないはず
    mock_time.time.assert_not_called()


@patch(MOCK_DYNAMODB_TABLE)
@patch(MOCK_TIME_MODULE)
def test_get_session_data_ttl_expired(mock_time, mock_db_table):
    """get_session_data: TTL が切れている場合に None"""
    session_id = "test-session-get-expired"
    current_time = 1700000000
    ttl_expired = current_time - 100  # 過去のTTL
    mock_time.time.return_value = current_time

    mock_item = {
        "sessionId": session_id,
        "ttl": Decimal(str(ttl_expired)),  # 期限切れのTTL
        "problem_data": {
            "Q1": {"questionId": "Q1", "correctAnswer": "A", "options": []}
        },  # ダミー
    }
    mock_db_table.get_item.return_value = {"Item": mock_item}

    session_data = get_session_data(session_id)

    assert session_data is None
    mock_db_table.get_item.assert_called_once_with(Key={"sessionId": session_id})
    mock_time.time.assert_called_once()  # TTL チェックのために呼ばれる


@patch(MOCK_DYNAMODB_TABLE)
def test_get_session_data_db_error(mock_db_table):
    """get_session_data: DynamoDB アクセスでエラーが発生した場合 ServiceError"""
    session_id = "test-session-get-dberror"
    from botocore.exceptions import ClientError

    mock_db_table.get_item.side_effect = ClientError({}, "GetItem")

    with pytest.raises(ServiceError) as excinfo:
        get_session_data(session_id)

    assert excinfo.value.status_code == 500
    assert "Failed to retrieve session data" in excinfo.value.detail
    mock_db_table.get_item.assert_called_once_with(Key={"sessionId": session_id})


@patch(MOCK_DYNAMODB_TABLE)
@patch(MOCK_TIME_MODULE)
def test_get_session_data_validation_error(mock_time, mock_db_table):
    """get_session_data: DB から取得したデータが不正でパースに失敗した場合 None"""
    session_id = "test-session-get-parse-error"
    current_time = 1700000000
    ttl_valid = current_time + 1000
    mock_time.time.return_value = current_time

    # problem_data の形式が不正 (例: correctAnswer がない) なアイテム
    mock_invalid_item = {
        "sessionId": session_id,
        "ttl": Decimal(str(ttl_valid)),
        "problem_data": {
            "Q1": {
                "questionId": "Q1",
                # "correctAnswer": "A", # 必須フィールドが欠けている
                "options": [],
            }
        },
    }
    mock_db_table.get_item.return_value = {"Item": mock_invalid_item}

    # get_session_data は内部で Validation Error を捕捉し None を返す実装
    session_data = get_session_data(session_id)

    assert session_data is None
    mock_db_table.get_item.assert_called_once_with(Key={"sessionId": session_id})
