# tests/test_main_utils.py
from app.main import shuffle_options  # shuffle_optionsがmainにあると仮定
from app.models import Explanation, Option, ProblemData  # 必要なモデルをインポート


def test_shuffle_options():
    """shuffle_optionsが選択肢のリストを返すこと、要素数が変わらないことを確認"""
    # ダミーの問題データを作成
    options_list = [
        Option(id="A", text="Option A"),
        Option(id="B", text="Option B"),
        Option(id="C", text="Option C"),
    ]
    dummy_explanation = Explanation(explanation="dummy")
    original_problem = ProblemData(
        questionId="Q001",
        bookSource="test",
        question="Test question",
        options=options_list.copy(),  # 元のリストを変更しないようにコピー
        correctAnswer="A",
        explanation=dummy_explanation,
    )
    problems = [original_problem]

    # 実行前の選択肢IDのセットを取得
    original_option_ids = {opt.id for opt in original_problem.options}

    # 関数を実行
    shuffled_problems = shuffle_options(problems)

# --- アサーション (検証) ---
    # リストが返ってくること
    assert isinstance(shuffled_problems, list)
    # 問題数が変わらないこと
    assert len(shuffled_problems) == 1

    shuffled_problem = shuffled_problems[0]
    # 選択肢の数が変わらないこと
    assert len(shuffled_problem.options) == len(options_list)

    # 選択肢のID構成が変わらないこと (順序は問わない)
    shuffled_option_ids = {opt.id for opt in shuffled_problem.options}
    assert shuffled_option_ids == original_option_ids

    # 元のリストの順序と異なる可能性があることを確認 (確率的なテスト - 常にパスするとは限らないが、簡単な指標)
    # 注意: 要素数が少ないとシャッフルしても同じ順序になる可能性もある
    if len(options_list) > 1:
        original_ids_order = [opt.id for opt in original_problem.options]
        shuffled_ids_order = [opt.id for opt in shuffled_problem.options]
        # 必ずしも異なるとは限らないが、多くの場合異なるはず
        # assert original_ids_order != shuffled_ids_order # これは不安定なのでコメントアウト推奨

    # 選択肢の中身 (text) が消えていないか確認 (一部)
    assert any(opt.text == "Option A" for opt in shuffled_problem.options)
