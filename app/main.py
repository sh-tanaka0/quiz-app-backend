import asyncio
import json
import random
import time
import uuid
from typing import Annotated, Dict, List, Literal

from botocore.exceptions import ClientError
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware  # <--- CORSMiddleware をインポート

from .aws_clients import dynamodb_table, s3_client
from .config import settings
from .models import (
    Answer,
    AnswerRequest,
    AnswerResponse,
    Option,
    ProblemData,
    Question,
    QuestionResponse,
    Result,
    SessionData,
    SessionDataItem,
)

app = FastAPI(title="Quiz App Backend")

# フロントエンドのローカル開発サーバーのオリジンを許可リストに追加
# 自分のフロントエンド開発環境のURLに合わせて変更してください
origins = [
    "http://localhost",  # ポート指定なし (通常は使わない)
    "http://localhost:3000",  # Create React App のデフォルトなど
    "http://localhost:5173",  # Vite のデフォルトなど
    "http://localhost:8080",  # Vue CLI のデフォルトなど
    # 必要に応じて他のローカル開発URLや、デプロイ後のフロントエンドURLを追加
    # 例: "https://your-frontend-domain.com"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # 許可するオリジン (上記リスト)
    allow_credentials=True,  # クッキーなどの認証情報を含むリクエストを許可するか (必要に応じて True)
    allow_methods=["*"],  # 許可するHTTPメソッド (GET, POST, etc.) "*"は全て許可
    allow_headers=["*"],  # 許可するHTTPヘッダー "*"は全て許可
)

# TTL設定 (秒単位、2時間)
SESSION_TTL_SECONDS = 2 * 60 * 60


class ServiceError(Exception):
    """サービス固有のエラーを表現するクラス"""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


@app.exception_handler(ServiceError)
async def service_exception_handler(request, exc: ServiceError):
    """ServiceErrorをHTTPExceptionに変換するハンドラ"""
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


async def get_files_from_s3(prefix: str) -> List[str]:
    """指定されたプレフィックスのJSON拡張子のファイルキー一覧をS3から取得"""
    keys = []
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=settings.s3_bucket_name, Prefix=prefix)
        for page in pages:
            if "Contents" in page:
                keys.extend(
                    [
                        obj["Key"]
                        for obj in page["Contents"]
                        if obj["Key"].endswith(".json")
                    ]
                )
    except ClientError as e:
        print(f"Error listing S3 objects: {e}")
        raise ServiceError(
            status_code=500, detail=f"Could not list questions from source: {prefix}"
        )
    return keys


async def read_s3_file_content(key: str) -> dict:
    """指定されたキーのファイル内容をS3から読み込む"""
    try:
        response = await asyncio.to_thread(
            s3_client.get_object, Bucket=settings.s3_bucket_name, Key=key
        )
        content = await asyncio.to_thread(response["Body"].read)
        return json.loads(content.decode("utf-8"))
    except ClientError as e:
        print(f"Error getting S3 object {key}: {e}")
        raise ServiceError(
            status_code=500, detail=f"Could not read question file: {key}"
        )
    except json.JSONDecodeError:
        print(f"Error decoding JSON from S3 object {key}")
        raise ServiceError(
            status_code=500, detail=f"Invalid format in question file: {key}"
        )


async def get_questions_from_s3(
    book_source: Literal["readable_code", "programming_principles", "both"], count: int
) -> List[ProblemData]:
    """S3から指定された条件でランダムに問題データを取得・パースする"""
    prefixes = []
    if book_source in ["readable_code", "both"]:
        prefixes.append("questions/readable_code/")
    if book_source in ["programming_principles", "both"]:
        prefixes.append("questions/programming_principles/")

    # 並列処理で全てのプレフィックスからファイルキーを取得
    all_keys = []
    tasks = [get_files_from_s3(prefix) for prefix in prefixes]
    results = await asyncio.gather(*tasks)
    for keys in results:
        all_keys.extend(keys)

    if not all_keys:
        raise ServiceError(
            status_code=404, detail=f"No questions found for source: {book_source}"
        )

    # count個のファイルをランダムに選択（重複なし）
    num_to_fetch = min(count, len(all_keys))
    if num_to_fetch < count:
        print(
            f"Warning: Requested {count} questions, but only {num_to_fetch} available for source '{book_source}'."
        )

    selected_keys = random.sample(all_keys, num_to_fetch)

    # 選択されたファイルの内容を非同期で取得
    content_tasks = [read_s3_file_content(key) for key in selected_keys]
    file_contents = await asyncio.gather(*content_tasks)

    # Pydanticモデルにパース
    problems = []
    for content in file_contents:
        try:
            problems.append(ProblemData.model_validate(content))
        except Exception as e:
            print(f"Error validating question data: {e}, content: {content}")
            continue

    if not problems:
        raise ServiceError(
            status_code=500, detail="Failed to load or validate any question data."
        )

    return problems


def shuffle_options(questions: List[ProblemData]) -> List[ProblemData]:
    """問題の選択肢をシャッフルする"""
    for q in questions:
        random.shuffle(q.options)
    return questions


def store_session_data(session_id: str, problems: List[ProblemData]) -> None:
    """DynamoDBにセッションデータを保存する"""
    current_time = int(time.time())
    ttl_timestamp = current_time + SESSION_TTL_SECONDS

    problem_data_map: Dict[str, SessionDataItem] = {}
    for problem in problems:
        problem_data_map[problem.questionId] = SessionDataItem(
            questionId=problem.questionId,
            correctAnswer=problem.correctAnswer,
            category=problem.category,
            question=problem.question,
            options=[Option(id=opt.id, text=opt.text) for opt in problem.options],
            explanation=problem.explanation.explanation,
        )

    session_data = SessionData(problem_data=problem_data_map, ttl=ttl_timestamp)

    try:
        dynamodb_table.put_item(
            Item={
                "sessionId": session_id,
                **session_data.model_dump(mode="json"),
            }
        )
        print(f"Session data stored for sessionId: {session_id}")
    except ClientError as e:
        print(f"Error storing session data to DynamoDB: {e}")
        raise ServiceError(status_code=500, detail="Failed to store session data.")


def get_session_data(session_id: str) -> SessionData:
    """DynamoDBからセッションデータを取得する"""
    try:
        response = dynamodb_table.get_item(Key={"sessionId": session_id})
        item = response.get("Item")
        if not item:
            print(f"Session data not found for sessionId: {session_id}")
            return None

        # TTLチェック
        current_time = int(time.time())
        if "ttl" in item and item["ttl"] < current_time:
            print(f"Session expired for sessionId: {session_id}")
            return None

        # 数値型をintに変換
        item["ttl"] = int(item["ttl"])

        # optionsを正しくパースする
        if "problem_data" in item:
            for qid, data in item["problem_data"].items():
                if "options" in data and isinstance(data["options"], list):
                    data["options"] = [
                        Option.model_validate(opt) for opt in data["options"]
                    ]

            # SessionDataItemにパース
            item["problem_data"] = {
                qid: SessionDataItem.model_validate(data)
                for qid, data in item["problem_data"].items()
            }

        return SessionData.model_validate(item)

    except ClientError as e:
        print(f"Error retrieving session data from DynamoDB for {session_id}: {e}")
        raise ServiceError(status_code=500, detail="Failed to retrieve session data.")
    except Exception as e:
        print(f"Error validating session data from DynamoDB for {session_id}: {e}")
        return None


def validate_answers(
    user_answers: List[Answer], session_data: SessionData
) -> List[Result]:
    """解答を検証・採点する"""
    results = []
    correct_data_map = session_data.problem_data

    for user_ans in user_answers:
        q_id = user_ans.questionId
        if q_id not in correct_data_map:
            print(
                f"Warning: Question ID {q_id} from user answer not found in session data."
            )
            continue

        correct_info = correct_data_map[q_id]

        # ユーザーが解答を未入力だった場合は不正解として処理
        if user_ans.answer is None:
            is_correct = False
        else:
            is_correct = user_ans.answer == correct_info.correctAnswer

        results.append(
            Result(
                questionId=q_id,
                category=correct_info.category,
                isCorrect=is_correct,
                userAnswer=user_ans.answer,
                correctAnswer=correct_info.correctAnswer,
                question=correct_info.question,
                options=correct_info.options,
                explanation=correct_info.explanation,
            )
        )
    return results


@app.get("/questions", response_model=QuestionResponse)
async def get_quiz_questions(
    bookSource: Annotated[
        Literal["readable_code", "programming_principles", "both"],
        Query(description="問題の出典"),
    ],
    count: Annotated[int, Query(ge=1, le=50, description="取得する問題数")],
    timeLimit: Annotated[
        int, Query(ge=10, le=300, description="1問あたりの制限時間(秒)")
    ],
):
    """問題セットを取得するエンドポイント"""
    try:
        # S3から問題取得
        problems_from_s3 = await get_questions_from_s3(bookSource, count)

        if not problems_from_s3:
            raise HTTPException(
                status_code=404,
                detail=f"No questions could be loaded for source: {bookSource}",
            )

        # 選択肢をシャッフル
        shuffled_problems = shuffle_options(problems_from_s3)

        # セッションID生成
        session_id = f"sess_{uuid.uuid4()}"

        # DynamoDBにセッション情報保存
        store_session_data(session_id, shuffled_problems)

        # レスポンス用の問題リスト作成
        response_questions = [
            Question(
                questionId=p.questionId,
                question=p.question,
                options=[Option(id=opt.id, text=opt.text) for opt in p.options],
            )
            for p in shuffled_problems
        ]

        return QuestionResponse(
            questions=response_questions,
            timeLimit=timeLimit * count,
            sessionId=session_id,
        )

    except HTTPException as http_exc:
        # すでに HTTPException である場合はそのまま re-raise する
        # これにより、上の raise HTTPException(404) が下の except Exception で捕捉されるのを防ぐ
        raise http_exc
    except ServiceError as e:
        # ServiceError はカスタムエラーとして処理
        # ServiceError 用のハンドラ (@app.exception_handler(ServiceError)) があれば
        # raise e でも良いが、ここでは直接 HTTPException に変換して raise する
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        # その他の予期せぬエラーは 500 として処理
        print(f"Unexpected error in get_quiz_questions: {e}")
        import traceback

        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while processing your request.",
        )


@app.post("/answers", response_model=AnswerResponse)
async def submit_answers(
    answer_request: Annotated[AnswerRequest, Body(description="ユーザーの解答")],
):
    """ユーザーの解答を検証し、結果を返すエンドポイント"""
    try:
        session_id = answer_request.sessionId
        user_answers = answer_request.answers

        # DynamoDBからセッションデータ取得
        session_data = get_session_data(session_id)

        # セッション検証
        if session_data is None:
            raise HTTPException(status_code=404, detail="Session not found or expired.")

        # 解答検証・採点
        results = validate_answers(user_answers, session_data)

        return AnswerResponse(results=results)

    except HTTPException as http_exc:
        # すでに HTTPException である場合はそのまま re-raise する
        # これにより、上の raise HTTPException(404) が下の except Exception で捕捉されるのを防ぐ
        raise http_exc
    except ServiceError as e:
        # ServiceError はカスタムエラーとして処理
        # ServiceError 用のハンドラ (@app.exception_handler(ServiceError)) があれば
        # raise e でも良いが、ここでは直接 HTTPException に変換して raise する
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        # その他の予期せぬエラーは 500 として処理
        print(f"Unexpected error in submit_answers: {e}")
        # スタックトレースも出力するとデバッグに役立つ
        import traceback

        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while processing your answers.",
        )


@app.get("/")
async def root():
    """ルートエンドポイント - サーバー稼働確認用"""
    return {"message": "Quiz App Backend is running!"}
