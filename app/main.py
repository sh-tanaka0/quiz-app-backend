import asyncio
import json
import random
import time
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Annotated, Any, Dict, List, Literal, Optional

import aioboto3
from aiobotocore.session import get_session
from botocore.exceptions import ClientError
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum

# .aws_clients は lifespan で管理するため不要になる想定
# from .aws_clients import dynamodb_table, s3_client # <-- 削除
from .config import settings  # settings モジュールが適切に設定されている前提
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

# --- Helper Function for DynamoDB Deserialization ---


def deserialize_dynamodb_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """DynamoDBのItemに含まれるDecimal型をint/floatに変換する"""
    if isinstance(item, list):
        return [deserialize_dynamodb_item(i) for i in item]
    elif isinstance(item, dict):
        new_dict = {}
        for k, v in item.items():
            new_dict[k] = deserialize_dynamodb_item(v)
        return new_dict
    elif isinstance(item, Decimal):
        if item % 1 == 0:
            return int(item)
        else:
            return float(item)
    else:
        return item


# --- Lifespan Event Handler for Async Clients ---


@asynccontextmanager
async def lifespan(app: FastAPI):
    # アプリケーション起動時: 非同期AWSクライアントを初期化
    print("Initializing async AWS clients...")
    session = get_session()
    # aioboto3リソースはコンテキストマネージャとして使うのが推奨される
    async with (
        session.create_client(
            "s3", region_name=settings.aws_default_region
        ) as s3_client,
        aioboto3.Session(region_name=settings.aws_default_region).resource(
            "dynamodb"
        ) as dynamodb_resource,
    ):
        app.state.s3_client = s3_client
        app.state.dynamodb_table = await dynamodb_resource.Table(
            settings.dynamodb_table_name
        )
        print("Async AWS clients initialized.")
        yield  # アプリケーション実行
    # アプリケーション終了時: クライアントは自動的にクリーンアップされる
    print("Async AWS clients cleaned up.")


# --- FastAPI Application ---

app = FastAPI(title="Quiz App Backend", lifespan=lifespan)

# CORS設定
origin = settings.frontend_origin
origins = [origin] if origin else ["*"]  # オリジンが未設定の場合は全て許可 (開発用)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# TTL設定 (秒単位、2時間)
SESSION_TTL_SECONDS = 2 * 60 * 60

# --- Custom Exception ---


class ServiceError(Exception):
    """サービス固有のエラーを表現するクラス"""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)  # エラーメッセージを設定


@app.exception_handler(ServiceError)
async def service_exception_handler(request: Request, exc: ServiceError):
    """ServiceErrorをHTTPExceptionに変換するハンドラ"""
    # ログ出力などをここに追加できる
    print(f"ServiceError caught: Status={exc.status_code}, Detail={exc.detail}")
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


# --- Async AWS Operations ---


async def get_files_from_s3(s3_client, bucket: str, prefix: str) -> List[str]:
    """指定されたプレフィックスのJSON拡張子のファイルキー一覧をS3から非同期で取得"""
    keys = []
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        async for result in paginator.paginate(Bucket=bucket, Prefix=prefix):
            if "Contents" in result:
                keys.extend(
                    [
                        obj["Key"]
                        for obj in result["Contents"]
                        if obj["Key"].endswith(".json")
                    ]
                )
    except ClientError as e:
        print(f"Error listing S3 objects with prefix '{prefix}': {e}")
        raise ServiceError(
            status_code=500, detail=f"Could not list questions from source: {prefix}"
        )
    except Exception as e:  # botocore以外の予期せぬエラーも捕捉
        print(f"Unexpected error listing S3 objects with prefix '{prefix}': {e}")
        raise ServiceError(
            status_code=500, detail="Unexpected error listing questions."
        )
    return keys


async def read_s3_file_content(s3_client, bucket: str, key: str) -> dict:
    """指定されたキーのファイル内容をS3から非同期で読み込む"""
    try:
        response = await s3_client.get_object(Bucket=bucket, Key=key)
        async with response["Body"] as stream:
            content = await stream.read()
        return json.loads(content.decode("utf-8"))
    except ClientError as e:
        print(f"Error getting S3 object {key}: {e}")
        # エラーレスポンスの詳細を取得しようと試みる (存在すれば)
        error_code = e.response.get("Error", {}).get("Code")
        if error_code == "NoSuchKey":
            raise ServiceError(
                status_code=404, detail=f"Question file not found: {key}"
            )
        else:
            raise ServiceError(
                status_code=500, detail=f"Could not read question file: {key}"
            )
    except json.JSONDecodeError:
        print(f"Error decoding JSON from S3 object {key}")
        raise ServiceError(
            status_code=500, detail=f"Invalid format in question file: {key}"
        )
    except Exception as e:  # botocore以外の予期せぬエラーも捕捉
        print(f"Unexpected error reading S3 object {key}: {e}")
        raise ServiceError(
            status_code=500, detail=f"Unexpected error reading question file: {key}"
        )


async def get_questions_from_s3(
    s3_client,
    bucket: str,
    book_source: Literal["readable_code", "programming_principles", "both"],
    count: int,
) -> List[ProblemData]:
    """S3から指定された条件でランダムに問題データを非同期で取得・パースする"""
    prefixes = []
    if book_source in ["readable_code", "both"]:
        prefixes.append("questions/readable_code/")
    if book_source in ["programming_principles", "both"]:
        prefixes.append("questions/programming_principles/")

    # 全てのプレフィックスからファイルキーを並列で取得
    key_tasks = [get_files_from_s3(s3_client, bucket, prefix) for prefix in prefixes]
    results = await asyncio.gather(*key_tasks, return_exceptions=True)  # エラーも収集

    all_keys = []
    for result in results:
        if isinstance(result, Exception):
            # get_files_from_s3 内で ServiceError を raise しているが、
            # gather で集めた場合、ここでもエラー処理が必要
            if isinstance(result, ServiceError):
                # ここでエラーを再 raise するか、ログに残して処理を続けるか選択
                # 今回はエラーが発生したら処理を中断するため再 raise
                raise result
            else:
                # 予期せぬエラー
                print(f"Unexpected error during key fetching: {result}")
                raise ServiceError(
                    status_code=500, detail="Error fetching question list."
                )
        else:
            all_keys.extend(result)

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
    content_tasks = [
        read_s3_file_content(s3_client, bucket, key) for key in selected_keys
    ]
    file_contents = await asyncio.gather(*content_tasks, return_exceptions=True)

    problems = []
    for i, content in enumerate(file_contents):
        key = selected_keys[i]  # エラー特定用
        if isinstance(content, Exception):
            if isinstance(content, ServiceError):
                # 特定のファイル読み込みエラーはログに残し、他の問題で処理を続行することも可能
                # ここではエラーが発生したら全体を失敗させる
                print(f"Error reading or processing file {key}: {content.detail}")
                raise content
            else:
                print(f"Unexpected error reading content for {key}: {content}")
                raise ServiceError(
                    status_code=500, detail=f"Unexpected error processing file: {key}"
                )
        else:
            try:
                problems.append(ProblemData.model_validate(content))
            except Exception as e:  # Pydantic validation errorなど
                print(
                    f"Error validating question data from {key}: {e}, content: {content}"
                )
                # バリデーションエラーが発生したファイルはスキップすることも可能
                # ここではエラーが発生したら全体を失敗させる
                raise ServiceError(
                    status_code=500,
                    detail=f"Invalid data format in question file: {key}",
                )

    if not problems:
        # ファイルはあったが、全て読み込み/パースに失敗した場合
        raise ServiceError(
            status_code=500, detail="Failed to load or validate any question data."
        )

    return problems


def shuffle_options(questions: List[ProblemData]) -> List[ProblemData]:
    """問題の選択肢をシャッフルする (同期処理でOK)"""
    for q in questions:
        random.shuffle(q.options)
    return questions


async def store_session_data(
    dynamodb_table, session_id: str, problems: List[ProblemData]
) -> None:
    """DynamoDBにセッションデータを非同期で保存する"""
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
            explanation=problem.explanation.explanation,  # 元コードに合わせて .explanation を追加
        )

    session_data = SessionData(problem_data=problem_data_map, ttl=ttl_timestamp)

    try:
        # Pydanticモデルをdictに変換 (JSON互換形式)
        item_to_store = session_data.model_dump(mode="json")
        # sessionId を追加
        item_to_store["sessionId"] = session_id

        await dynamodb_table.put_item(Item=item_to_store)
        print(f"Session data stored for sessionId: {session_id}")
    except ClientError as e:
        print(f"Error storing session data to DynamoDB for {session_id}: {e}")
        raise ServiceError(status_code=500, detail="Failed to store session data.")
    except Exception as e:  # その他の予期せぬエラー
        print(f"Unexpected error storing session data for {session_id}: {e}")
        raise ServiceError(
            status_code=500, detail="Unexpected error storing session data."
        )


async def get_session_data(dynamodb_table, session_id: str) -> Optional[SessionData]:
    """DynamoDBからセッションデータを非同期で取得する"""
    try:
        response = await dynamodb_table.get_item(Key={"sessionId": session_id})
        item = response.get("Item")

        if not item:
            print(f"Session data not found for sessionId: {session_id}")
            return None  # ServiceError ではなく None を返す (呼び出し元で404処理)

        # DynamoDBのDecimal型などをPythonの型に変換
        deserialized_item = deserialize_dynamodb_item(item)

        # TTLチェック
        current_time = int(time.time())
        if "ttl" in deserialized_item and deserialized_item["ttl"] < current_time:
            print(f"Session expired for sessionId: {session_id}")
            # TTL切れの場合も削除はDynamoDBのTTL機能に任せ、ここではNoneを返す
            return None  # ServiceError ではなく None を返す (呼び出し元で404処理)

        # Pydanticモデルにパース (ここでバリデーションも行われる)
        return SessionData.model_validate(deserialized_item)

    except ClientError as e:
        print(f"Error retrieving session data from DynamoDB for {session_id}: {e}")
        raise ServiceError(status_code=500, detail="Failed to retrieve session data.")
    except Exception as e:  # Pydantic validation errorなど
        print(
            f"Error validating or processing session data from DynamoDB for {session_id}: {e}"
        )
        # データ破損の可能性もあるため、Noneではなくエラーとする
        raise ServiceError(status_code=500, detail="Failed to process session data.")


def validate_answers(
    user_answers: List[Answer], session_data: SessionData
) -> List[Result]:
    """解答を検証・採点する (同期処理でOK)"""
    results = []
    correct_data_map = session_data.problem_data

    for user_ans in user_answers:
        q_id = user_ans.questionId
        if q_id not in correct_data_map:
            print(
                f"Warning: Question ID {q_id} from user answer not found in session data."
            )
            # 不明な questionId は結果に含めない、またはエラーとするか選択
            # ここではスキップ
            continue

        correct_info = correct_data_map[q_id]

        # ユーザーが解答を未入力だった場合は不正解として処理
        is_correct = False
        if user_ans.answer is not None:  # null でないことを確認
            is_correct = user_ans.answer == correct_info.correctAnswer

        results.append(
            Result(
                questionId=q_id,
                category=correct_info.category,
                isCorrect=is_correct,
                userAnswer=user_ans.answer,
                correctAnswer=correct_info.correctAnswer,
                question=correct_info.question,
                options=correct_info.options,  # SessionDataItemから取得
                explanation=correct_info.explanation,  # SessionDataItemから取得
            )
        )
    return results


# --- API Endpoints ---


@app.get("/questions", response_model=QuestionResponse)
async def get_quiz_questions(
    request: Request,  # lifespan で初期化されたクライアントにアクセスするため
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
    s3_client = request.app.state.s3_client
    dynamodb_table = request.app.state.dynamodb_table
    bucket_name = settings.s3_bucket_name

    try:
        # S3から問題取得 (非同期)
        problems_from_s3 = await get_questions_from_s3(
            s3_client, bucket_name, bookSource, count
        )
        # problems_from_s3 は空でないことが get_questions_from_s3 内で保証される

        # 選択肢をシャッフル (同期)
        shuffled_problems = shuffle_options(problems_from_s3)

        # セッションID生成
        session_id = f"sess_{uuid.uuid4()}"

        # DynamoDBにセッション情報保存 (非同期)
        await store_session_data(dynamodb_table, session_id, shuffled_problems)

        # レスポンス用の問題リスト作成 (同期)
        response_questions = [
            Question(
                questionId=p.questionId,
                question=p.question,
                # correctAnswer をレスポンスに含めない
                options=[Option(id=opt.id, text=opt.text) for opt in p.options],
            )
            for p in shuffled_problems
        ]

        return QuestionResponse(
            questions=response_questions,
            timeLimit=timeLimit
            * len(response_questions),  # 実際に取得できた問題数で計算
            sessionId=session_id,
        )

    except ServiceError as e:
        # ServiceError はハンドラで処理されるので再 raise
        raise e
    except Exception as e:
        # 予期せぬエラー
        print(f"Unexpected error in get_quiz_questions: {e}")
        import traceback

        traceback.print_exc()
        # ServiceError にラップしてハンドラに処理させる
        raise ServiceError(
            status_code=500,
            detail="An unexpected error occurred while processing your request.",
        )


@app.post("/answers", response_model=AnswerResponse)
async def submit_answers(
    request: Request,  # lifespan で初期化されたクライアントにアクセスするため
    answer_request: Annotated[AnswerRequest, Body(description="ユーザーの解答")],
):
    """ユーザーの解答を検証し、結果を返すエンドポイント"""
    dynamodb_table = request.app.state.dynamodb_table
    session_id = answer_request.sessionId
    user_answers = answer_request.answers

    try:
        # DynamoDBからセッションデータ取得 (非同期)
        session_data = await get_session_data(dynamodb_table, session_id)

        # セッション検証
        if session_data is None:
            # get_session_data で None が返された場合 (Not Found or Expired)
            raise ServiceError(status_code=404, detail="Session not found or expired.")

        # 解答検証・採点 (同期)
        results = validate_answers(user_answers, session_data)

        return AnswerResponse(results=results)

    except ServiceError as e:
        # ServiceError はハンドラで処理されるので再 raise
        raise e
    except Exception as e:
        # 予期せぬエラー
        print(f"Unexpected error in submit_answers: {e}")
        import traceback

        traceback.print_exc()
        # ServiceError にラップしてハンドラに処理させる
        raise ServiceError(
            status_code=500,
            detail="An unexpected error occurred while processing your answers.",
        )


@app.get("/")
async def root():
    """ルートエンドポイント - サーバー稼働確認用"""
    return {"message": "Quiz App Backend is running!"}


# Mangum handler (lifespan="on" または "auto")
# Lambda 環境で lifespan を有効にする
handler = Mangum(app, lifespan="on")
