import json
import os
import random  # ランダム要素を使う場合 (今回は固定パターンで生成)

# --- 設定 ---
NUM_TO_GENERATE_PER_CATEGORY = 29  # RC001/PP001 以外に生成する数
OUTPUT_BASE_DIR = "sample_data/questions"  # 保存先ベースディレクトリ
RC_DIR = os.path.join(OUTPUT_BASE_DIR, "readable_code")
PP_DIR = os.path.join(OUTPUT_BASE_DIR, "programming_principles")

# --- ディレクトリ作成 ---
os.makedirs(RC_DIR, exist_ok=True)
os.makedirs(PP_DIR, exist_ok=True)

# --- readable_code 用データ生成テンプレート ---
base_question_rc = (
    "コードの可読性向上のための「{}」について、最も適切な説明は次のうちどれですか？"
)
base_options_rc = {
    "A": "「{}」はコードの一貫性を保つための基本です。",
    "B": "「{}」を適用する前に、まずコードの単純化を検討すべきです。",
    "C": "「{}」は特に大規模なプロジェクトやチーム開発で重要になります。",
    "D": "「{}」を過剰に意識すると、かえって読みにくくなる場合があります。",
}
base_explanation_rc = "「{}」は、読みやすく保守しやすいコードを書く上で重要な概念です。選択肢 {} がこの原則の意図を最もよく反映しています。なぜなら..."
rc_topics = [
    "意味のある変数名",
    "関数の適切な分割",
    "マジックナンバーの排除",
    "早期リターンパターン",
    "一貫性のあるインデント",
    "適切なコメントの使用",
    "コードフォーマッタの活用",
    "クラスの凝集度",
    "モジュールの結合度",
    "テストコードの可読性",
    "リファクタリング戦略",
    "デザインパターンの導入",
    "API設計の原則",
    "効果的なログ出力",
    "設定値の外部化",
    "状態管理の簡潔化",
    "非同期処理の明瞭化",
    "ライブラリ選択の基準",
    "バージョン管理のブランチ戦略",
    "READMEの充実",
    "コードレビュー文化",
    "パフォーマンスに関する誤解",
    "セキュリティの基本",
    "ビルドツールの選定",
    "デプロイ手順の自動化",
    "インフラ構成の文書化",
    "監視とアラート設定",
    "障害発生時の対応フロー",
] * 2  # トピックが足りないので循環させる

# --- programming_principles 用データ生成テンプレート ---
base_question_pp = "ソフトウェア開発原則「{}」に関して、最も的確な記述はどれですか？"
base_options_pp = {
    "A": "「{}」は主にコードの再利用性を高めることを目的とします。",
    "B": "「{}」はソフトウェアの保守性を維持するための重要な指針です。",
    "C": "「{}」は将来的な機能拡張を容易にするために考慮されます。",
    "D": "「{}」を適用することで、コードのテスト容易性が向上します。",
}
base_explanation_pp = "「{}」は、堅牢で柔軟なソフトウェアを構築するための基本原則の一つです。選択肢 {} はこの原則の核心的な考え方を示しています。これにより..."
pp_topics = [
    "DRY (Don't Repeat Yourself)",
    "KISS (Keep It Simple, Stupid)",
    "YAGNI (You Ain't Gonna Need It)",
    "単一責任の原則 (SRP)",
    "オープン/クローズド原則 (OCP)",
    "リスコフの置換原則 (LSP)",
    "インターフェース分離の原則 (ISP)",
    "依存関係逆転の原則 (DIP)",
    "カプセル化",
    "情報隠蔽",
    "疎結合",
    "高凝集",
    "コマンド・クエリ分離 (CQS)",
    "契約による設計 (DbC)",
    "関心の分離 (SoC)",
    "デメテルの法則",
    "冪等性 (Idempotency)",
    "純粋関数",
    "参照透過性",
    "副作用の管理",
    "状態遷移のモデル化",
    "エラーハンドリングの方針",
    "ログレベルの標準化",
    "設定管理のベストプラクティス",
    "非機能要件の定義",
    "技術的負債の認識",
    "進化的なアーキテクチャ",
    "テスト駆動開発 (TDD)",
    "振る舞い駆動開発 (BDD)",
] * 2  # トピックが足りないので循環させる

# --- 正解選択肢のパターン ---
correct_answers_cycle = ["A", "B", "C", "D"]


# --- ファイル生成関数 ---
def generate_files(
    category_prefix,
    book_source,
    category_name,
    base_question,
    base_options,
    base_explanation,
    topics,
    output_dir,
):
    print(f"Generating sample data for {book_source}...")
    for i in range(2, NUM_TO_GENERATE_PER_CATEGORY + 2):  # 002 から 030 まで
        q_id = f"{category_prefix}{i:03d}"
        topic_index = i - 2
        # トピックリストが足りない場合に備えて循環させる
        topic = topics[topic_index % len(topics)]
        # 正解を循環させる (カテゴリごとでパターンを変える)
        correct_ans_id = correct_answers_cycle[
            (i + (0 if category_prefix == "RC" else 1)) % 4
        ]

        question_text = base_question.format(topic)
        options = [
            {"id": opt_id, "text": base_options[opt_id].format(topic)}
            for opt_id in ["A", "B", "C", "D"]
        ]
        explanation_text = base_explanation.format(topic, correct_ans_id)
        # ダミーのページ番号
        ref_page_start = (50 if category_prefix == "RC" else 90) + (
            i * random.randint(2, 4)
        )
        ref_pages = f"{ref_page_start}-{ref_page_start + random.randint(1, 5)}"

        data = {
            "questionId": q_id,
            "bookSource": book_source,
            "category": category_name,
            "question": question_text,
            "options": options,
            "correctAnswer": correct_ans_id,
            "explanation": {
                "explanation": explanation_text,
                "referencePages": ref_pages,
                "additionalResources": [],  # 今回は空
            },
        }

        filepath = os.path.join(output_dir, f"{q_id}.json")
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except IOError as e:
            print(f"Error writing file {filepath}: {e}")

    print(f"Generated {NUM_TO_GENERATE_PER_CATEGORY} sample files in {output_dir}")


# --- 実行 ---
generate_files(
    "RC",
    "readable_code",
    "readability",
    base_question_rc,
    base_options_rc,
    base_explanation_rc,
    rc_topics,
    RC_DIR,
)
generate_files(
    "PP",
    "programming_principles",
    "principles",
    base_question_pp,
    base_options_pp,
    base_explanation_pp,
    pp_topics,
    PP_DIR,
)

print("Sample data generation complete.")
