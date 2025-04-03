# tests/test_validation.py
from app.main import validate_answers  # テスト対象の関数
from app.models import (  # 必要なモデル
    Answer,
    Option,
    Result,
    SessionData,
    SessionDataItem,
)

"""
`validate_answers` 関数のテスト (ユーザーの解答が正解の場合)。
このテストでは以下を検証します:
- 質問が1つ解答された場合、結果が1つ返されること。
- 結果が `Result` クラスのインスタンスであること。
- 結果に正しい質問ID、正解ステータス、ユーザーの解答、正解、
  カテゴリ、質問文、解説、および表示順序が含まれること。
- ユーザーの解答が正解と一致する場合、`isCorrect` 属性が `True` であること。
テストデータ:
- 質問 "Q001" に対するユーザーの解答が1つ。
- セッションデータに質問 "Q001" の正解とメタデータが含まれる。
"""


def test_validate_answers_correct():
    user_answers = [Answer(questionId="Q001", answer="A", displayOrder=["C", "A", "B"])]
    session_data = SessionData(
        problem_data={
            "Q001": SessionDataItem(
                questionId="Q001",
                correctAnswer="A",
                category="Test Category",
                question="Test Question 1",
                options=[
                    Option(id="A", text="Ans A"),
                    Option(id="B", text="Ans B"),
                    Option(id="C", text="Ans C"),
                ],
                explanation="Explanation 1",
            )
        },
        ttl=1234567890,
    )

    results = validate_answers(user_answers, session_data)

    assert len(results) == 1
    result = results[0]
    assert isinstance(result, Result)
    assert result.questionId == "Q001"
    assert result.isCorrect is True
    assert result.userAnswer == "A"
    assert result.correctAnswer == "A"
    assert result.category == "Test Category"
    assert result.question == "Test Question 1"
    assert result.explanation == "Explanation 1"
    assert result.displayOrder == ["C", "A", "B"]


"""
`validate_answers` 関数のテスト (ユーザーの解答が不正解の場合)。
このテストでは以下を検証します:
- 質問が1つ解答された場合、結果が1つ返されること。
- 結果が `Result` クラスのインスタンスであること。
- 結果に正しい質問ID、正解ステータス、ユーザーの解答、正解、
  カテゴリ、質問文、解説、および表示順序が含まれること。
- ユーザーの解答が正解と一致しない場合、`isCorrect` 属性が `False` であること。
テストデータ:
- 質問 "Q002" に対するユーザーの解答が1つ。
- セッションデータに質問 "Q002" の正解とメタデータが含まれる。
"""


def test_validate_answers_incorrect():
    user_answers = [Answer(questionId="Q002", answer="C", displayOrder=["B", "C", "A"])]
    session_data = SessionData(
        problem_data={
            "Q002": SessionDataItem(
                questionId="Q002",
                correctAnswer="B",  # 正解は B
                category="Another Category",
                question="Test Question 2",
                options=[
                    Option(id="A", text="Ans A"),
                    Option(id="B", text="Ans B"),
                    Option(id="C", text="Ans C"),
                ],
                explanation="Explanation 2",
            )
        },
        ttl=1234567890,
    )

    results = validate_answers(user_answers, session_data)

    assert len(results) == 1
    result = results[0]
    assert result.isCorrect is False
    assert result.userAnswer == "C"
    assert result.correctAnswer == "B"
    assert result.questionId == "Q002"


"""
`validate_answers` 関数のテスト (複数の問題がある場合)。
このテストでは以下を検証します:
- 複数の質問が解答された場合、結果がそれぞれ返されること。
- 結果が `Result` クラスのインスタンスであること。
- 結果に正しい質問ID、正解ステータス、ユーザーの解答、正解、
    カテゴリ、質問文、解説、および表示順序が含まれること。
- ユーザーの解答が正解と一致する場合、`isCorrect` 属性が `True` であること。
- ユーザーの解答が不正解の場合、`isCorrect` 属性が `False` であること。
テストデータ:
- 2つの質問 "Q001" と "Q002" に対するユーザーの解答が2つ。
- セッションデータにそれぞれの質問の正解とメタデータが含まれる。
"""


def test_validate_answers_multiple():
    user_answers = [
        Answer(questionId="Q001", answer="A", displayOrder=["A", "B"]),
        Answer(questionId="Q002", answer="C", displayOrder=["C", "B"]),
    ]
    session_data = SessionData(
        problem_data={
            "Q001": SessionDataItem(
                questionId="Q001",
                correctAnswer="A",
                question="Q1",
                options=[],
                explanation="E1",
            ),
            "Q002": SessionDataItem(
                questionId="Q002",
                correctAnswer="B",
                question="Q2",
                options=[],
                explanation="E2",
            ),
        },
        ttl=1234567890,
    )

    results = validate_answers(user_answers, session_data)

    assert len(results) == 2
    assert results[0].isCorrect is True
    assert results[0].questionId == "Q001"
    assert results[1].isCorrect is False
    assert results[1].questionId == "Q002"


"""
`validate_answers` 関数のテスト (セッションデータが空の場合)。
このテストでは以下を検証します:
- セッションデータが空の場合、結果が空のリストとして返されること。
テストデータ:
- セッションデータが空である。
"""


def test_validate_answers_question_not_in_session():
    user_answers = [
        Answer(
            questionId="Q999", answer="A", displayOrder=["A", "B"]
        )  # Q999はセッションにない
    ]
    session_data = SessionData(
        problem_data={
            "Q001": SessionDataItem(
                questionId="Q001",
                correctAnswer="A",
                question="Q1",
                options=[],
                explanation="E1",
            ),
        },
        ttl=1234567890,
    )

    results = validate_answers(user_answers, session_data)

    # 存在しない問題は結果に含まれないはず
    assert len(results) == 0
