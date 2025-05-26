import asyncio
import logging
import random
import time  # store_session_data と get_session_data で time.time() を使用するため残します
import uuid
from typing import Annotated, Any, Dict, List, Literal, Optional

import aioboto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError
from fastapi import (
    Body,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # INFOレベルのログは出力される可能性があります

_dynamodb_deserializer = TypeDeserializer()


def deserialize_dynamodb_item_fully(item: Dict[str, Any]) -> Dict[str, Any]:
    if not item:
        return {}
    python_native_item = {}
    for key, value in item.items():
        python_native_item[key] = _dynamodb_deserializer.deserialize(value)
    return python_native_item


# --- グローバルAWSクライアントの宣言 ---
_aws_dynamodb_client: Optional[Any] = None
_aws_quiz_problems_table: Optional[Any] = None
_aws_session_data_table: Optional[Any] = None
_client_init_lock = asyncio.Lock()


async def _initialize_global_aws_clients():
    """
    グローバルAWSクライアントを一度だけ非同期に初期化する内部関数。
    """
    global _aws_dynamodb_client, _aws_quiz_problems_table, _aws_session_data_table

    async with _client_init_lock:
        if _aws_dynamodb_client is None:
            logger.info(
                "Initializing AWS clients globally..."
            )  # 初期化のログは残します
            session = aioboto3.Session(region_name=settings.aws_default_region)

            temp_dynamodb_client = session.client(
                "dynamodb", region_name=settings.aws_default_region
            )
            _aws_dynamodb_client = await temp_dynamodb_client.__aenter__()

            temp_dynamodb_resource = session.resource(
                "dynamodb", region_name=settings.aws_default_region
            )
            entered_dynamodb_resource = await temp_dynamodb_resource.__aenter__()

            _aws_quiz_problems_table = await entered_dynamodb_resource.Table(
                settings.dynamodb_quiz_problems_table_name
            )
            _aws_session_data_table = await entered_dynamodb_resource.Table(
                settings.dynamodb_session_table_name
            )
            logger.info(
                "AWS clients initialized globally."
            )  # 初期化完了のログは残します
        else:
            logger.info(
                "AWS clients already initialized globally."
            )  # 既に初期化済みの場合のログも残します


# --- 依存性注入用のゲッター関数 ---
async def get_dynamodb_client() -> Any:
    if _aws_dynamodb_client is None:
        await _initialize_global_aws_clients()
    if _aws_dynamodb_client is None:
        raise HTTPException(
            status_code=503,
            detail="DynamoDB client not available after initialization attempt.",
        )
    return _aws_dynamodb_client


async def get_quiz_problems_table() -> Any:
    if _aws_quiz_problems_table is None:
        await _initialize_global_aws_clients()
    if _aws_quiz_problems_table is None:
        raise HTTPException(
            status_code=503,
            detail="Quiz problems table not available after initialization attempt.",
        )
    return _aws_quiz_problems_table


async def get_session_data_table() -> Any:
    if _aws_session_data_table is None:
        await _initialize_global_aws_clients()
    if _aws_session_data_table is None:
        raise HTTPException(
            status_code=503,
            detail="Session data table not available after initialization attempt.",
        )
    return _aws_session_data_table


# --- FastAPI Application ---
app = FastAPI(title="Quiz App Backend")


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
    logger.error(
        f"ServiceError caught: Status={exc.status_code}, Detail={exc.detail}",
        exc_info=True,  # エラーのスタックトレースを出力するために残します
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


# --- DynamoDB Operations for Quiz Problems ---
async def get_questions_from_dynamodb(
    dynamodb_client: Annotated[Any, Depends(get_dynamodb_client)],
    quiz_problems_table: Annotated[Any, Depends(get_quiz_problems_table)],
    book_source: Literal["readable_code", "programming_principles", "both"],
    count: int,
) -> List[ProblemData]:
    target_book_sources: List[str] = []
    if book_source == "both":
        target_book_sources.extend(["readable_code", "programming_principles"])
    else:
        target_book_sources.append(book_source)

    all_question_ids_from_gsi: List[str] = []

    try:
        query_tasks = []
        for src in target_book_sources:
            query_tasks.append(
                quiz_problems_table.query(
                    IndexName=settings.gsi_book_source_index_name,
                    KeyConditionExpression=Key("bookSource").eq(src),
                    ProjectionExpression="questionId",
                )
            )
        query_results = await asyncio.gather(*query_tasks, return_exceptions=True)

        for idx, result_item in enumerate(query_results):
            current_source = target_book_sources[idx]
            if isinstance(result_item, Exception):
                logger.error(
                    f"Error querying question IDs for '{current_source}' from GSI: {result_item}"
                )
                raise ServiceError(
                    status_code=500,
                    detail=f"Could not retrieve question ID list for '{current_source}'.",
                )

            items_from_query = result_item.get("Items", [])
            for item_id_obj in items_from_query:
                all_question_ids_from_gsi.append(item_id_obj["questionId"])

            if "LastEvaluatedKey" in result_item:
                logger.warning(  # ページネーション未対応の警告は運用上有用な場合があるので残します
                    f"Warning: More items available for bookSource {current_source}, but pagination not fully implemented."
                )
    except ClientError as e:
        logger.error(f"DynamoDB ClientError while querying GSI: {e}", exc_info=True)
        raise ServiceError(
            status_code=500,
            detail="Error communicating with database for question IDs.",
        )
    except Exception as e:
        logger.error(
            f"Unexpected error while fetching question IDs: {e}", exc_info=True
        )
        raise ServiceError(
            status_code=500, detail="Unexpected error fetching question IDs."
        )

    if not all_question_ids_from_gsi:
        # logger.warning(f"No question IDs found from GSI for source: {book_source}") # ServiceErrorでカバー
        raise ServiceError(
            status_code=404, detail=f"No questions found for source: {book_source}"
        )

    num_to_fetch = min(count, len(all_question_ids_from_gsi))
    if num_to_fetch < count:
        logger.warning(  # リクエスト数と取得可能数の差異の警告は残します
            f"Warning: Requested {count} questions, but only {num_to_fetch} available for source '{book_source}'."
        )

    if num_to_fetch == 0:
        return []

    selected_ids = random.sample(all_question_ids_from_gsi, num_to_fetch)
    problems: List[ProblemData] = []
    if not selected_ids:
        return problems

    try:
        keys_for_batch_get = [{"questionId": {"S": q_id}} for q_id in selected_ids]
        request_items = {
            settings.dynamodb_quiz_problems_table_name: {
                "Keys": keys_for_batch_get,
            }
        }
        response = await dynamodb_client.batch_get_item(RequestItems=request_items)

        raw_problems_from_db = response.get("Responses", {}).get(
            settings.dynamodb_quiz_problems_table_name, []
        )

        if response.get("UnprocessedKeys", {}).get(
            settings.dynamodb_quiz_problems_table_name
        ):
            logger.warning(  # UnprocessedKeysの警告は残します
                "Warning: BatchGetItem returned UnprocessedKeys, some items may not have been retrieved. Retry logic not implemented."
            )
    except ClientError as e:
        logger.error(f"DynamoDB ClientError during BatchGetItem: {e}", exc_info=True)
        raise ServiceError(
            status_code=500, detail="Error fetching problem details from database."
        )
    except Exception as e:
        logger.error(f"Unexpected error during BatchGetItem: {e}", exc_info=True)
        raise ServiceError(
            status_code=500, detail="Unexpected error fetching problem details."
        )

    parsed_count = 0
    for item_dict in raw_problems_from_db:
        try:
            python_native_dict = deserialize_dynamodb_item_fully(item_dict)
            problems.append(ProblemData.model_validate(python_native_dict))
            parsed_count += 1
        except Exception as e:
            logger.error(  # バリデーションエラーのログは重要なので残します
                f"Error validating problem data from DynamoDB: {e}, item_id: {item_dict.get('questionId', {}).get('S')}",
                exc_info=True,
            )
            continue  # 1つのアイテムのパースエラーで全体を失敗させない

    if not problems and num_to_fetch > 0:
        logger.error(
            "Failed to load or validate any question data after fetching, though IDs were selected."
        )
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
    session_dynamodb_table: Annotated[Any, Depends(get_session_data_table)],
    session_id: str,
    problems: List[ProblemData],
) -> None:
    current_time = int(time.time())  # time.time() を使用
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
    except ClientError as e:
        logger.error(
            f"Error storing session data to DynamoDB for {session_id}: {e}",
            exc_info=True,
        )
        raise ServiceError(status_code=500, detail="Failed to store session data.")
    except Exception as e:
        logger.error(
            f"Unexpected error storing session data for {session_id}: {e}",
            exc_info=True,
        )
        raise ServiceError(
            status_code=500, detail="Unexpected error storing session data."
        )


async def get_session_data(
    session_dynamodb_table: Annotated[Any, Depends(get_session_data_table)],
    session_id: str,
) -> Optional[SessionData]:
    try:
        response = await session_dynamodb_table.get_item(Key={"sessionId": session_id})
        item = response.get("Item")
        if not item:
            logger.warning(
                f"Session data not found for sessionId: {session_id}"
            )  # セッションが見つからない警告は残します
            return None
        current_time = int(time.time())  # time.time() を使用
        if "ttl" in item and item["ttl"] < current_time:
            logger.info(  # TTL切れのログは残します
                f"Session {session_id} has expired (TTL: {item['ttl']}, Current: {current_time})."
            )
            return None
        validated_data = SessionData.model_validate(item)
        return validated_data
    except ClientError as e:
        logger.error(
            f"Failed to retrieve session data for {session_id} from DynamoDB: {e}",
            exc_info=True,
        )
        raise ServiceError(status_code=500, detail="Failed to retrieve session data.")
    except Exception as e:
        logger.error(
            f"Failed to process or validate session data for {session_id}: {e}",
            exc_info=True,
        )
        raise ServiceError(status_code=500, detail="Failed to process session data.")


def validate_answers(
    user_answers: List[Answer], session_data: SessionData
) -> List[Result]:
    results = []
    correct_data_map = session_data.problem_data
    for user_ans in user_answers:
        q_id = user_ans.questionId
        if q_id not in correct_data_map:
            logger.warning(  # ユーザー解答のIDがセッションにない警告は残します
                f"Question ID {q_id} from user answer not found in session data. Skipping."
            )
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
                explanation=correct_info.explanation,
            )
        )
    return results


# --- API Endpoints ---
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
    dynamodb_client_injected: Annotated[Any, Depends(get_dynamodb_client)],
    quiz_problems_table_injected: Annotated[Any, Depends(get_quiz_problems_table)],
    session_data_table_injected: Annotated[Any, Depends(get_session_data_table)],
    request: Request,
):
    aws_request_id = "N/A"
    if "aws.lambda_context" in request.scope:
        aws_request_id = request.scope["aws.lambda_context"].aws_request_id
    logger.info(  # エンドポイント開始ログは残します
        f"[ReqID: {aws_request_id}] GET /questions: START - bookSource='{bookSource}', count={count}, timeLimitPerQuestion={timeLimit}"
    )

    try:
        problems_from_db = await get_questions_from_dynamodb(
            dynamodb_client_injected, quiz_problems_table_injected, bookSource, count
        )

        if not problems_from_db:
            # logger.warning( # ServiceErrorでカバー
            # f"[ReqID: {aws_request_id}] /questions: No problems returned from get_questions_from_dynamodb for source: {bookSource}"
            # )
            raise ServiceError(
                status_code=404,
                detail=f"No questions could be loaded for source: {bookSource}. Please try a different source or smaller count.",
            )

        shuffled_problems = shuffle_options(problems_from_db)
        session_id = f"sess_{uuid.uuid4()}"
        await store_session_data(
            session_data_table_injected, session_id, shuffled_problems
        )

        response_questions = [
            Question(
                questionId=p.questionId,
                question=p.question,
                options=[Option(id=opt.id, text=opt.text) for opt in p.options],
            )
            for p in shuffled_problems
        ]

        final_response = QuestionResponse(
            questions=response_questions,
            timeLimit=timeLimit * len(response_questions),
            sessionId=session_id,
        )
        logger.info(  # エンドポイント終了ログは残します
            f"[ReqID: {aws_request_id}] GET /questions: END - Successfully processed. Returning {len(response_questions)} questions."
        )
        return final_response
    except ServiceError as e:
        logger.error(  # ServiceErrorは専用ハンドラでログ出力されるが、リクエストIDを含めるためにここでもログ出力
            f"[ReqID: {aws_request_id}] GET /questions: ServiceError. Status: {e.status_code}, Detail: {e.detail}"
        )
        raise e  # service_exception_handler に処理を委譲
    except Exception as e:
        logger.critical(  # 予期せぬエラーのログは重要なので残します
            f"[ReqID: {aws_request_id}] GET /questions: CRITICAL - Unexpected error: {e}",
            exc_info=True,
        )
        raise ServiceError(  # クライアントには汎用的なエラーメッセージを返す
            status_code=500,
            detail="An unexpected error occurred while processing your request.",
        )


@app.post("/answers", response_model=AnswerResponse)
async def submit_answers(
    answer_request: Annotated[AnswerRequest, Body(description="ユーザーの解答")],
    session_data_table_injected: Annotated[Any, Depends(get_session_data_table)],
    request: Request,
):
    aws_request_id = "N/A"
    if "aws.lambda_context" in request.scope:
        aws_request_id = request.scope["aws.lambda_context"].aws_request_id
    logger.info(  # エンドポイント開始ログは残します
        f"[ReqID: {aws_request_id}] POST /answers: START - sessionId='{answer_request.sessionId}', num_answers={len(answer_request.answers)}"
    )

    session_id = answer_request.sessionId
    user_answers = answer_request.answers

    try:
        session_data = await get_session_data(session_data_table_injected, session_id)

        if session_data is None:
            # logger.warning( # ServiceErrorでカバー
            # f"[ReqID: {aws_request_id}] /answers: Session not found or expired for sessionId: {session_id}"
            # )
            raise ServiceError(status_code=404, detail="Session not found or expired.")

        results = validate_answers(user_answers, session_data)
        response = AnswerResponse(results=results)
        logger.info(  # エンドポイント終了ログは残します
            f"[ReqID: {aws_request_id}] POST /answers: END - Successfully processed."
        )
        return response
    except ServiceError as e:
        logger.error(  # ServiceErrorは専用ハンドラでログ出力されるが、リクエストIDを含めるためにここでもログ出力
            f"[ReqID: {aws_request_id}] POST /answers: ServiceError. Status: {e.status_code}, Detail: {e.detail}"
        )
        raise e  # service_exception_handler に処理を委譲
    except Exception as e:
        logger.critical(  # 予期せぬエラーのログは重要なので残します
            f"[ReqID: {aws_request_id}] POST /answers: CRITICAL - Unexpected error: {e}",
            exc_info=True,
        )
        raise ServiceError(  # クライアントには汎用的なエラーメッセージを返す
            status_code=500,
            detail="An unexpected error occurred while processing your answers.",
        )


@app.get("/")
async def root():
    logger.info("Root path '/' accessed.")  # ルートパスへのアクセスログは残します
    return {"message": "Quiz App Backend is running!"}


# Mangumハンドラ
handler = Mangum(app, lifespan="off")
