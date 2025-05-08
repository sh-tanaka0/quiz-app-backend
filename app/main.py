import asyncio
import random
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Any, Dict, List, Literal, Optional

import aioboto3
from boto3.dynamodb.conditions import Key  # DynamoDBクエリ条件のためインポート
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum

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

# --- Helper Function for DynamoDB Deserialization ---
# DynamoDBの型記述子をアンラップしてPythonネイティブ型に変換する
_dynamodb_deserializer = TypeDeserializer()


def deserialize_dynamodb_item_fully(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    DynamoDBのアイテム (型記述子付き) を、Pythonネイティブな型の辞書に完全に変換する。
    例: {'S': 'value'} -> 'value', {'N': '123'} -> Decimal('123'),
         {'L': [{'S': 'a'}]} -> ['a']
    この関数はトップレベルの辞書を受け取り、各値をデシリアライズします。
    注意: TypeDeserializer().deserialize は Decimal 型を返すので、
          必要であればさらに int/float に変換する処理を ProblemData モデルのバリデーションや
          この関数の後段で行うか、Pydanticのカスタムシリアライザ/バリデータで対応します。
    """
    if not item:
        return {}
    python_native_item = {}
    for key, value in item.items():
        python_native_item[key] = _dynamodb_deserializer.deserialize(value)
    return python_native_item


# --- Lifespan Event Handler for Async Clients ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Initializing async AWS clients...")

    aio_boto_3_session = aioboto3.Session(
        region_name=settings.aws_default_region
    )  # aioboto3 session for DynamoDB

    async with (
        aio_boto_3_session.client(
            "dynamodb", region_name=settings.aws_default_region
        ) as dynamodb_client,
        aio_boto_3_session.resource(
            "dynamodb", region_name=settings.aws_default_region
        ) as dynamodb_resource,
    ):
        app.state.dynamodb_client = (
            dynamodb_client  # BatchGetItem などクライアントレベル操作用
        )

        app.state.quiz_problems_table = await dynamodb_resource.Table(
            settings.dynamodb_quiz_problems_table_name
        )
        app.state.session_data_table = await dynamodb_resource.Table(
            settings.dynamodb_session_table_name
        )

        print("Async AWS clients initialized.")
        yield
    print("Async AWS clients cleaned up.")


# --- FastAPI Application ---
app = FastAPI(title="Quiz App Backend", lifespan=lifespan)

# CORS設定
origin = settings.frontend_origin
origins = [origin] if origin and origin != "None" and origin != "" else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SESSION_TTL_SECONDS = 2 * 60 * 60


class ServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


@app.exception_handler(ServiceError)
async def service_exception_handler(request: Request, exc: ServiceError):
    print(f"ServiceError caught: Status={exc.status_code}, Detail={exc.detail}")
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


# --- DynamoDB Operations for Quiz Problems  ---
async def get_questions_from_dynamodb(
    request: Request,
    book_source: Literal["readable_code", "programming_principles", "both"],
    count: int,
) -> List[ProblemData]:
    quiz_problems_table = request.app.state.quiz_problems_table
    dynamodb_client = request.app.state.dynamodb_client

    target_book_sources: List[str] = []
    if book_source == "both":
        target_book_sources.extend(["readable_code", "programming_principles"])
    else:
        target_book_sources.append(book_source)

    all_question_ids_from_gsi: List[str] = []

    try:
        query_tasks = []
        for src in target_book_sources:
            print(
                f"Querying GSI '{settings.gsi_book_source_index_name}' for bookSource: {src}"
            )
            query_tasks.append(
                quiz_problems_table.query(
                    IndexName=settings.gsi_book_source_index_name,
                    KeyConditionExpression=Key("bookSource").eq(src),
                    ProjectionExpression="questionId",
                )
            )

        # GSIクエリを並行実行
        query_results = await asyncio.gather(*query_tasks, return_exceptions=True)

        for idx, result_item in enumerate(query_results):
            current_source = target_book_sources[idx]
            if isinstance(result_item, Exception):
                print(
                    f"Error querying question IDs for '{current_source}' from GSI: {result_item}"
                )
                raise ServiceError(
                    status_code=500,
                    detail=f"Could not retrieve question ID list for '{current_source}'.",
                )

            items_from_query = result_item.get("Items", [])
            print(
                f"Found {len(items_from_query)} question IDs for bookSource: {current_source}"
            )
            for item_id_obj in items_from_query:
                all_question_ids_from_gsi.append(item_id_obj["questionId"])

            # TODO:将来的にはDynamoDBクエリのページネーション処理を追加したい
            # LastEvaluatedKey が存在する場合、それを使って再度クエリを実行するループが必要。
            # 今回は簡略化のため、最初のページのアイテムのみを処理します。
            # if 'LastEvaluatedKey' in result_item:
            #     print(f"Warning: More items available for bookSource {current_source}, but pagination not fully implemented in this example.")

    except ClientError as e:
        print(f"DynamoDB ClientError while querying GSI: {e}")
        raise ServiceError(
            status_code=500,
            detail="Error communicating with database for question IDs.",
        )
    except Exception as e:
        print(f"Unexpected error while fetching question IDs: {e}")
        raise ServiceError(
            status_code=500, detail="Unexpected error fetching question IDs."
        )

    if not all_question_ids_from_gsi:
        raise ServiceError(
            status_code=404, detail=f"No questions found for source: {book_source}"
        )

    num_to_fetch = min(count, len(all_question_ids_from_gsi))
    if num_to_fetch < count:
        print(
            f"Warning: Requested {count} questions, but only {num_to_fetch} available for source '{book_source}'."
        )

    if num_to_fetch == 0:
        return []

    selected_ids = random.sample(all_question_ids_from_gsi, num_to_fetch)
    print(f"Selected {len(selected_ids)} question IDs for BatchGetItem.")

    problems: List[ProblemData] = []
    if not selected_ids:
        return problems

    try:
        # BatchGetItemで問題データを取得 (1回のリクエストで最大100アイテム)
        # selected_ids が100を超える場合は分割してリクエストする必要がある。
        # ここでは num_to_fetch (したがって selected_ids) が100以下と仮定 (APIのcount上限が50なので問題なし)。
        keys_for_batch_get = [{"questionId": {"S": q_id}} for q_id in selected_ids]

        request_items = {
            settings.dynamodb_quiz_problems_table_name: {  # テーブル名を直接指定
                "Keys": keys_for_batch_get,
            }
        }

        response = await dynamodb_client.batch_get_item(RequestItems=request_items)

        raw_problems_from_db = response.get("Responses", {}).get(
            settings.dynamodb_quiz_problems_table_name, []
        )
        print(f"Retrieved {len(raw_problems_from_db)} items from BatchGetItem.")

        # TODO:将来的にはUnprocessedKeysの処理を追加したい
        # response.get('UnprocessedKeys', {}).get(settings.dynamodb_quiz_problems_table_name)
        # があれば再試行ロジックを追加。今回は簡略化。

    except ClientError as e:
        print(f"DynamoDB ClientError during BatchGetItem: {e}")
        raise ServiceError(
            status_code=500, detail="Error fetching problem details from database."
        )
    except Exception as e:
        print(f"Unexpected error during BatchGetItem: {e}")
        raise ServiceError(
            status_code=500, detail="Unexpected error fetching problem details."
        )

    for item_dict in raw_problems_from_db:
        try:
            # DynamoDBの型記述子をアンラップしてPythonネイティブな辞書に変換
            python_native_dict = deserialize_dynamodb_item_fully(item_dict)

            # さらに、もし ProblemData モデルが Decimal ではなく int/float を期待しているなら、
            # 既存の deserialize_dynamodb_item を使って Decimal を変換する
            # (ただし、deserialize_dynamodb_item は型記述子をアンラップしないので、
            #  deserialize_dynamodb_item_fully の後に適用するのは適切ではない。
            #  Pydantic が Decimal を扱えるので、この変換は不要な可能性が高い)
            # final_dict_for_pydantic = deserialize_dynamodb_item(python_native_dict) # 既存のヘルパーの適用方法を再考

            # PydanticはDecimalを適切に扱えるはずなので、python_native_dict をそのまま渡す
            problems.append(ProblemData.model_validate(python_native_dict))
        except Exception as e:
            print(
                f"Error validating problem data from DynamoDB: {e}, item: {item_dict.get('questionId')}"
            )
            # エラーアイテムはスキップ (またはエラーをraise)
            continue

    if not problems and num_to_fetch > 0:  # IDはあったがデータ取得/パースに全て失敗
        raise ServiceError(
            status_code=500,
            detail="Failed to load or validate any question data after fetching.",
        )
    return problems


def shuffle_options(questions: List[ProblemData]) -> List[ProblemData]:
    for q in questions:
        if hasattr(q, "options") and q.options:
            random.shuffle(q.options)
    return questions


async def store_session_data(
    session_dynamodb_table, session_id: str, problems: List[ProblemData]
) -> None:
    current_time = int(time.time())
    ttl_timestamp = current_time + SESSION_TTL_SECONDS

    problem_data_map: Dict[str, SessionDataItem] = {}
    for problem in problems:
        explanation_text = None
        if problem.explanation and hasattr(problem.explanation, "explanation"):
            explanation_text = problem.explanation.explanation

        problem_data_map[problem.questionId] = SessionDataItem(
            questionId=problem.questionId,
            correctAnswer=problem.correctAnswer,
            category=problem.category,
            question=problem.question,
            options=[Option(id=opt.id, text=opt.text) for opt in problem.options],
            explanation=explanation_text,
        )
    session_data = SessionData(problem_data=problem_data_map, ttl=ttl_timestamp)
    try:
        item_to_store = session_data.model_dump(mode="json")
        item_to_store["sessionId"] = session_id
        await session_dynamodb_table.put_item(Item=item_to_store)
        print(f"Session data stored for sessionId: {session_id}")
    except ClientError as e:
        print(f"Error storing session data to DynamoDB for {session_id}: {e}")
        raise ServiceError(status_code=500, detail="Failed to store session data.")
    except Exception as e:
        print(f"Unexpected error storing session data for {session_id}: {e}")
        raise ServiceError(
            status_code=500, detail="Unexpected error storing session data."
        )


async def get_session_data(
    session_dynamodb_table, session_id: str
) -> Optional[SessionData]:
    try:
        response = await session_dynamodb_table.get_item(Key={"sessionId": session_id})
        item = response.get("Item")
        if not item:
            return None
        deserialized_item = deserialize_dynamodb_item_fully(item)
        current_time = int(time.time())
        if "ttl" in deserialized_item and deserialized_item["ttl"] < current_time:
            return None
        return SessionData.model_validate(deserialized_item)
    except ClientError:
        raise ServiceError(status_code=500, detail="Failed to retrieve session data.")
    except Exception:
        raise ServiceError(status_code=500, detail="Failed to process session data.")


def validate_answers(
    user_answers: List[Answer], session_data: SessionData
) -> List[Result]:
    results = []
    correct_data_map = session_data.problem_data
    for user_ans in user_answers:
        q_id = user_ans.questionId
        if q_id not in correct_data_map:
            continue
        correct_info = correct_data_map[q_id]
        is_correct = False
        if user_ans.answer is not None:
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
                explanation=correct_info.explanation,  # SessionDataItem.explanation (str)
            )
        )
    return results


# --- API Endpoints (修正箇所: get_quiz_questions) ---
@app.get("/questions", response_model=QuestionResponse)
async def get_quiz_questions(
    request: Request,  # lifespan で初期化されたクライアント/テーブルにアクセスするため
    bookSource: Annotated[
        Literal["readable_code", "programming_principles", "both"],
        Query(description="問題の出典"),
    ],
    count: Annotated[int, Query(ge=1, le=50, description="取得する問題数")],
    timeLimit: Annotated[
        int, Query(ge=10, le=300, description="1問あたりの制限時間(秒)")
    ],
):
    session_dynamodb_table = request.app.state.session_data_table

    try:
        # DynamoDBから問題取得
        problems_from_db = await get_questions_from_dynamodb(request, bookSource, count)

        if not problems_from_db:
            raise ServiceError(
                status_code=404,
                detail=f"No questions could be loaded for source: {bookSource}",
            )

        shuffled_problems = shuffle_options(problems_from_db)
        session_id = f"sess_{uuid.uuid4()}"
        await store_session_data(session_dynamodb_table, session_id, shuffled_problems)

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
            timeLimit=timeLimit
            * len(response_questions),  # 実際に取得できた問題数で計算
            sessionId=session_id,
        )
    except ServiceError as e:
        raise e
    except Exception as e:
        print(f"Unexpected error in get_quiz_questions: {e}")
        import traceback

        traceback.print_exc()
        raise ServiceError(
            status_code=500,
            detail="An unexpected error occurred while processing your request.",
        )


@app.post("/answers", response_model=AnswerResponse)
async def submit_answers(
    request: Request,  # lifespan で初期化されたクライアント/テーブルにアクセスするため
    answer_request: Annotated[AnswerRequest, Body(description="ユーザーの解答")],
):
    """ユーザーの解答を検証し、結果を返すエンドポイント"""
    session_dynamodb_table = request.app.state.session_data_table
    session_id = answer_request.sessionId
    user_answers = answer_request.answers
    try:
        session_data = await get_session_data(session_dynamodb_table, session_id)
        if session_data is None:
            raise ServiceError(status_code=404, detail="Session not found or expired.")
        results = validate_answers(user_answers, session_data)
        return AnswerResponse(results=results)
    except ServiceError as e:
        raise e
    except Exception as e:
        print(f"Unexpected error in submit_answers: {e}")
        import traceback

        traceback.print_exc()
        raise ServiceError(
            status_code=500,
            detail="An unexpected error occurred while processing your answers.",
        )


@app.get("/")
async def root():
    return {"message": "Quiz App Backend is running!"}


handler = Mangum(app, lifespan="on")
