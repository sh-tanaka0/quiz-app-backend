# tests/test_validation.py
from typing import Dict, List  # 型ヒント用

import pytest  # pytest をインポート

from app.main import validate_answers  # テスト対象の関数
from app.models import (  # 必要なモデル
    Answer,
    Option,
    Result,
    SessionData,
    SessionDataItem,
)

# --- Fixtures for Test Data ---


@pytest.fixture
def base_session_data_item_q1() -> SessionDataItem:
    """基本的な SessionDataItem (Q001, 正解 A) を生成する Fixture"""
    return SessionDataItem(
        questionId="Q001",
        correctAnswer="A",
        category="Test Category 1",
        question="Test Question 1",
        options=[
            Option(id="A", text="Ans A"),
            Option(id="B", text="Ans B"),
            Option(id="C", text="Ans C"),
        ],
        explanation="Explanation 1",
    )


@pytest.fixture
def base_session_data_item_q2() -> SessionDataItem:
    """基本的な SessionDataItem (Q002, 正解 B) を生成する Fixture"""
    return SessionDataItem(
        questionId="Q002",
        correctAnswer="B",  # 正解は B
        category="Test Category 2",
        question="Test Question 2",
        options=[
            Option(id="A", text="Ans A"),
            Option(id="B", text="Ans B"),
            Option(id="C", text="Ans C"),
        ],
        explanation="Explanation 2",
    )


@pytest.fixture
def create_session_data_fixture():
    """SessionData を生成する Fixture ファクトリ"""

    def _create_session_data(items: List[SessionDataItem]) -> SessionData:
        problem_data: Dict[str, SessionDataItem] = {
            item.questionId: item for item in items
        }
        # テスト実行において ttl の値自体は validate_answers では使われないため固定値でOK
        return SessionData(problem_data=problem_data, ttl=9999999999)

    return _create_session_data


# --- Existing Tests (Refactored using Fixtures) ---


def test_validate_answers_correct(
    base_session_data_item_q1, create_session_data_fixture
):
    """ユーザーの解答が正解の場合"""
    user_answers = [Answer(questionId="Q001", answer="A", displayOrder=["C", "A", "B"])]
    # Fixture を使ってセッションデータを作成
    session_data = create_session_data_fixture([base_session_data_item_q1])
    # 元のセッションデータアイテムを比較用に取得
    item = base_session_data_item_q1

    results = validate_answers(user_answers, session_data)

    # アサーションは元のテストと同様だが、比較対象として fixture の値を使う
    assert len(results) == 1
    result = results[0]
    assert isinstance(result, Result)
    assert result.questionId == item.questionId
    assert result.isCorrect is True
    assert result.userAnswer == "A"
    assert result.correctAnswer == item.correctAnswer
    assert result.category == item.category
    assert result.question == item.question
    assert result.explanation == item.explanation
    assert result.options == item.options  # options も含まれることを確認
    assert result.displayOrder == ["C", "A", "B"]


def test_validate_answers_incorrect(
    base_session_data_item_q2, create_session_data_fixture
):
    """ユーザーの解答が不正解の場合"""
    user_answers = [Answer(questionId="Q002", answer="C", displayOrder=["B", "C", "A"])]
    session_data = create_session_data_fixture([base_session_data_item_q2])
    item = base_session_data_item_q2

    results = validate_answers(user_answers, session_data)

    assert len(results) == 1
    result = results[0]
    assert isinstance(result, Result)
    assert result.questionId == item.questionId
    assert result.isCorrect is False
    assert result.userAnswer == "C"
    assert result.correctAnswer == item.correctAnswer  # 正解は 'B'
    assert result.category == item.category
    assert result.question == item.question
    assert result.explanation == item.explanation
    assert result.options == item.options
    assert result.displayOrder == ["B", "C", "A"]


def test_validate_answers_multiple(
    base_session_data_item_q1, base_session_data_item_q2, create_session_data_fixture
):
    """複数の問題がある場合 (正解と不正解が混在)"""
    user_answers = [
        Answer(questionId="Q001", answer="A", displayOrder=["A", "B"]),  # 正解
        Answer(questionId="Q002", answer="C", displayOrder=["C", "B"]),  # 不正解
    ]
    # Q001 と Q002 のデータを含むセッションを作成
    session_data = create_session_data_fixture(
        [base_session_data_item_q1, base_session_data_item_q2]
    )

    results = validate_answers(user_answers, session_data)

    assert len(results) == 2

    # 結果の順序は user_answers の順序に依存すると仮定
    # Result for Q001
    res1 = results[0]
    item1 = base_session_data_item_q1
    assert res1.questionId == item1.questionId
    assert res1.isCorrect is True
    assert res1.userAnswer == "A"
    assert res1.correctAnswer == item1.correctAnswer
    assert res1.question == item1.question
    assert res1.options == item1.options
    assert res1.explanation == item1.explanation
    assert res1.category == item1.category
    assert res1.displayOrder == ["A", "B"]

    # Result for Q002
    res2 = results[1]
    item2 = base_session_data_item_q2
    assert res2.questionId == item2.questionId
    assert res2.isCorrect is False
    assert res2.userAnswer == "C"
    assert res2.correctAnswer == item2.correctAnswer  # 正解は 'B'
    assert res2.question == item2.question
    assert res2.options == item2.options
    assert res2.explanation == item2.explanation
    assert res2.category == item2.category
    assert res2.displayOrder == ["C", "B"]


def test_validate_answers_question_not_in_session(
    base_session_data_item_q1, create_session_data_fixture
):
    """ユーザー解答の questionId がセッションデータに存在しない場合"""
    user_answers = [
        Answer(
            questionId="Q999", answer="A", displayOrder=["A", "B"]
        )  # Q999 はセッションにない
    ]
    # セッションには Q001 しか含まない
    session_data = create_session_data_fixture([base_session_data_item_q1])

    results = validate_answers(user_answers, session_data)

    # 存在しない問題IDは無視され、結果リストは空になるはず
    assert len(results) == 0
    assert results == []


# --- New Tests ---


def test_validate_answers_empty_input():
    """ユーザーの解答リストが空の場合"""
    user_answers: List[Answer] = []
    # セッションデータはダミー (problem_data が空でも良い)
    session_data = SessionData(problem_data={}, ttl=9999999999)

    results = validate_answers(user_answers, session_data)

    # 結果リストも空になるはず
    assert results == []


def test_validate_answers_empty_session_problems(create_session_data_fixture):
    """セッションデータの problem_data が空の場合"""
    user_answers = [Answer(questionId="Q001", answer="A", displayOrder=["A", "B"])]
    # problem_data が空のセッションデータを作成
    session_data = create_session_data_fixture([])

    results = validate_answers(user_answers, session_data)

    # 解答に対応する問題がセッションにないため、結果リストは空になるはず
    assert results == []


def test_validate_answers_duplicate_question_id(
    base_session_data_item_q1, create_session_data_fixture
):
    """ユーザー解答に重複した questionId がある場合"""
    user_answers = [
        Answer(questionId="Q001", answer="A", displayOrder=["A", "B"]),  # 正解
        Answer(
            questionId="Q001", answer="B", displayOrder=["B", "A"]
        ),  # 不正解 (同じID)
    ]
    session_data = create_session_data_fixture([base_session_data_item_q1])

    results = validate_answers(user_answers, session_data)

    # 現在の実装 (main.py の想定) では、両方の解答が処理されるはず
    assert len(results) == 2

    # 1つ目の結果 (userAnswer='A')
    assert results[0].questionId == "Q001"
    assert results[0].isCorrect is True
    assert results[0].userAnswer == "A"
    assert results[0].correctAnswer == "A"
    assert results[0].displayOrder == ["A", "B"]

    # 2つ目の結果 (userAnswer='B')
    assert results[1].questionId == "Q001"
    assert results[1].isCorrect is False
    assert results[1].userAnswer == "B"
    assert results[1].correctAnswer == "A"  # 正解は同じ 'A'
    assert results[1].displayOrder == ["B", "A"]

    # correctAnswer や question など他のフィールドも両方の結果に含まれる
    assert results[0].question == base_session_data_item_q1.question
    assert results[1].question == base_session_data_item_q1.question


def test_validate_answers_invalid_user_answer(
    base_session_data_item_q1, create_session_data_fixture
):
    """ユーザー解答の answer が選択肢に存在しない不正な値の場合"""
    user_answers = [
        Answer(
            questionId="Q001", answer="X", displayOrder=["A", "B", "C"]
        )  # 'X' は不正な選択肢ID
    ]
    session_data = create_session_data_fixture(
        [base_session_data_item_q1]
    )  # 正解は 'A'

    results = validate_answers(user_answers, session_data)

    # 不正な解答IDでもエラーにはならず、単に不正解として扱われるはず (現在の実装想定)
    assert len(results) == 1
    result = results[0]
    assert result.questionId == "Q001"
    assert result.isCorrect is False
    assert result.userAnswer == "X"  # ユーザーが送信した値がそのまま入る
    assert result.correctAnswer == "A"  # 正解は 'A'
    assert result.question == base_session_data_item_q1.question
    assert result.options == base_session_data_item_q1.options
    assert result.explanation == base_session_data_item_q1.explanation
    assert result.displayOrder == ["A", "B", "C"]
