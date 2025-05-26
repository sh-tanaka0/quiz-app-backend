import asyncio
import logging
import random
import time
import uuid
from typing import Annotated, Any, Dict, List, Literal, Optional

import aioboto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError
from fastapi import (  # Depends を追加
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
logger.setLevel(logging.INFO)

_dynamodb_deserializer = TypeDeserializer()


def deserialize_dynamodb_item_fully(item: Dict[str, Any]) -> Dict[str, Any]:
    if not item:
        return {}
    python_native_item = {}
    for key, value in item.items():
        python_native_item[key] = _dynamodb_deserializer.deserialize(value)
    return python_native_item


# --- グローバルAWSクライアントの宣言 ---
# これらのクライアントは、最初のアクセス時に非同期で初期化されます。
_aws_dynamodb_client: Optional[Any] = None
_aws_quiz_problems_table: Optional[Any] = None
_aws_session_data_table: Optional[Any] = None
_client_init_lock = asyncio.Lock()  # 初期化処理の競合を防ぐためのロック


async def _initialize_global_aws_clients():
    """
    グローバルAWSクライアントを一度だけ非同期に初期化する内部関数。
    """
    global _aws_dynamodb_client, _aws_quiz_problems_table, _aws_session_data_table

    async with _client_init_lock:  # 複数箇所から同時に初期化が試みられるのを防ぐ
        if _aws_dynamodb_client is None:  # まだ初期化されていなければ
            logger.info("GLOBAL_INIT: Initializing AWS clients globally...")
            start_time = time.perf_counter()

            session = aioboto3.Session(region_name=settings.aws_default_region)
            # aioboto3.Session.client() と resource() はコンテキストマネージャとして使用することを推奨
            # しかし、グローバルクライアントとして永続化するため、ここでは直接 `await __aenter__()` のような形で
            # 取得するか、あるいはセッションから直接クライアントとリソースを作成し、
            # Lambda の終了時にクリーンアップする方法がないことを受け入れる (Lambda環境では通常問題にならない)。
            # ここでは簡潔さのため、コンテキストマネージャなしで取得します。
            # 実際には、アプリケーション終了時にこれらのリソースをクローズする仕組みがないことに注意。
            # Lambdaでは実行環境が破棄される際に自動的にクリーンアップされることを期待します。

            # DynamoDBクライアントの初期化
            # 一時的なクライアントとリソースを作成して、そこから永続的なクライアントとテーブルオブジェクトを取得
            temp_dynamodb_client = session.client(
                "dynamodb", region_name=settings.aws_default_region
            )
            _aws_dynamodb_client = (
                await temp_dynamodb_client.__aenter__()
            )  # Manually enter context

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

            # 注意: 上記の __aenter__ で取得したクライアント/リソースは、対応する __aexit__ を呼び出すべきですが、
            # グローバルオブジェクトとして保持する場合、そのタイミングが難しいです。
            # Lambdaのライフサイクルに依存する形となります。
            # より堅牢なのは、セッションオブジェクトをグローバルに保持し、必要に応じてクライアント/リソースを生成することかもしれません。
            # しかし、ここではレポートの「グローバルクライアント初期化」の趣旨に沿って、クライアントオブジェクト自体をグローバルに保持します。

            end_time = time.perf_counter()
            logger.info(
                f"GLOBAL_INIT: AWS clients initialized globally in {end_time - start_time:.4f} seconds."
            )
        else:
            logger.info("GLOBAL_INIT: AWS clients already initialized globally.")


# --- 依存性注入用のゲッター関数 ---
async def get_dynamodb_client() -> Any:
    if _aws_dynamodb_client is None:
        await _initialize_global_aws_clients()
    if _aws_dynamodb_client is None:  # 初期化失敗の場合
        raise HTTPException(
            status_code=503,
            detail="DynamoDB client not available after initialization attempt.",
        )
    return _aws_dynamodb_client


async def get_quiz_problems_table() -> Any:
    if _aws_quiz_problems_table is None:
        await _initialize_global_aws_clients()
    if _aws_quiz_problems_table is None:  # 初期化失敗の場合
        raise HTTPException(
            status_code=503,
            detail="Quiz problems table not available after initialization attempt.",
        )
    return _aws_quiz_problems_table


async def get_session_data_table() -> Any:
    if _aws_session_data_table is None:
        await _initialize_global_aws_clients()
    if _aws_session_data_table is None:  # 初期化失敗の場合
        raise HTTPException(
            status_code=503,
            detail="Session data table not available after initialization attempt.",
        )
    return _aws_session_data_table


# --- Lifespan Event Handler for Async Clients (コメントアウトまたは削除) ---
# Mangum(lifespan="off") を使用するため、FastAPIのlifespanはトリガーされません。
# クライアント初期化は上記のグローバルスコープで行われます。
"""
@asynccontextmanager
async def lifespan(app: FastAPI):
    # このlifespan関数は Mangum(lifespan="off") のため実行されません。
    # もし実行された場合のログを残しておくこともできますが、混乱を避けるためコメントアウト推奨。
    logger.info("FastAPI Lifespan event: START (This should NOT run if Mangum lifespan='off')")
    
    # --- ここに以前の初期化ロジックがあった ---
    # app.state.dynamodb_client = ...
    # app.state.quiz_problems_table = ...
    # app.state.session_data_table = ...
    
    yield
    logger.info("FastAPI Lifespan event: END (This should NOT run if Mangum lifespan='off')")
"""

# --- FastAPI Application ---
# app = FastAPI(title="Quiz App Backend", lifespan=lifespan) # lifespan引数を削除
app = FastAPI(title="Quiz App Backend")


# CORS設定 (変更なし)
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
        exc_info=True,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


# --- DynamoDB Operations for Quiz Problems (クライアント取得方法を変更) ---
async def get_questions_from_dynamodb(
    # request: Request, # app.stateを使わないので不要に
    dynamodb_client: Annotated[Any, Depends(get_dynamodb_client)],  # 依存性注入で取得
    quiz_problems_table: Annotated[
        Any, Depends(get_quiz_problems_table)
    ],  # 依存性注入で取得
    book_source: Literal["readable_code", "programming_principles", "both"],
    count: int,
) -> List[ProblemData]:
    func_start_time = time.perf_counter()
    logger.info(
        f"get_questions_from_dynamodb: START - Fetching questions for bookSource='{book_source}', count={count}"
    )
    # グローバルクライアントを直接使用 (または依存性注入されたものを使用)
    # quiz_problems_table = request.app.state.quiz_problems_table # 変更
    # dynamodb_client = request.app.state.dynamodb_client       # 変更

    # ... (以降のロジックはほぼ変更なし、request.app.stateへのアクセス部分のみ修正)
    target_book_sources: List[str] = []
    if book_source == "both":
        target_book_sources.extend(["readable_code", "programming_principles"])
    else:
        target_book_sources.append(book_source)

    all_question_ids_from_gsi: List[str] = []

    try:
        gsi_query_start_time = time.perf_counter()
        query_tasks = []
        for src in target_book_sources:
            logger.info(
                f"Querying GSI '{settings.gsi_book_source_index_name}' for bookSource: {src}"
            )
            query_tasks.append(
                quiz_problems_table.query(  # quiz_problems_table を直接使用
                    IndexName=settings.gsi_book_source_index_name,
                    KeyConditionExpression=Key("bookSource").eq(src),
                    ProjectionExpression="questionId",
                )
            )
        # ... (残りの get_questions_from_dynamodb の内容は、クライアントの参照方法以外は既存のものを維持) ...
        query_results = await asyncio.gather(*query_tasks, return_exceptions=True)
        gsi_query_end_time = time.perf_counter()
        logger.info(
            f"get_questions_from_dynamodb: GSI queries completed in {gsi_query_end_time - gsi_query_start_time:.4f} seconds."
        )

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
            logger.info(
                f"Found {len(items_from_query)} question IDs for bookSource: {current_source}"
            )
            for item_id_obj in items_from_query:
                all_question_ids_from_gsi.append(item_id_obj["questionId"])

            if "LastEvaluatedKey" in result_item:
                logger.warning(
                    f"Warning: More items available for bookSource {current_source}, but pagination not fully implemented in this example."
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
        logger.warning(f"No question IDs found from GSI for source: {book_source}")
        raise ServiceError(
            status_code=404, detail=f"No questions found for source: {book_source}"
        )

    num_to_fetch = min(count, len(all_question_ids_from_gsi))
    if num_to_fetch < count:
        logger.warning(
            f"Warning: Requested {count} questions, but only {num_to_fetch} available for source '{book_source}'."
        )

    if num_to_fetch == 0:
        logger.info("get_questions_from_dynamodb: END - No questions to fetch.")
        return []

    selected_ids = random.sample(all_question_ids_from_gsi, num_to_fetch)
    logger.info(f"Selected {len(selected_ids)} random question IDs for BatchGetItem.")

    problems: List[ProblemData] = []
    if not selected_ids:
        logger.info(
            "get_questions_from_dynamodb: END - No selected IDs, returning empty list."
        )
        return problems

    try:
        batch_get_start_time = time.perf_counter()
        keys_for_batch_get = [{"questionId": {"S": q_id}} for q_id in selected_ids]
        request_items = {
            settings.dynamodb_quiz_problems_table_name: {
                "Keys": keys_for_batch_get,
            }
        }
        response = await dynamodb_client.batch_get_item(
            RequestItems=request_items
        )  # dynamodb_client を直接使用
        batch_get_end_time = time.perf_counter()
        logger.info(
            f"get_questions_from_dynamodb: BatchGetItem completed in {batch_get_end_time - batch_get_start_time:.4f} seconds."
        )

        raw_problems_from_db = response.get("Responses", {}).get(
            settings.dynamodb_quiz_problems_table_name, []
        )
        logger.info(f"Retrieved {len(raw_problems_from_db)} items from BatchGetItem.")

        if response.get("UnprocessedKeys", {}).get(
            settings.dynamodb_quiz_problems_table_name
        ):
            logger.warning(
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

    deserialization_validation_start_time = time.perf_counter()
    parsed_count = 0
    for item_dict in raw_problems_from_db:
        try:
            python_native_dict = deserialize_dynamodb_item_fully(item_dict)
            problems.append(ProblemData.model_validate(python_native_dict))
            parsed_count += 1
        except Exception as e:
            logger.error(
                f"Error validating problem data from DynamoDB: {e}, item_id: {item_dict.get('questionId', {}).get('S')}",
                exc_info=True,
            )
            continue
    deserialization_validation_end_time = time.perf_counter()
    logger.info(
        f"get_questions_from_dynamodb: Deserialization and validation of {parsed_count} items completed in {deserialization_validation_end_time - deserialization_validation_start_time:.4f} seconds."
    )

    if not problems and num_to_fetch > 0:
        logger.error(
            "Failed to load or validate any question data after fetching, though IDs were selected."
        )
        raise ServiceError(
            status_code=500,
            detail="Failed to load or validate any question data after fetching.",
        )

    func_end_time = time.perf_counter()
    logger.info(
        f"get_questions_from_dynamodb: END - Processed {len(problems)} problems in {func_end_time - func_start_time:.4f} seconds."
    )
    return problems


def shuffle_options(questions: List[ProblemData]) -> List[ProblemData]:
    for q in questions:
        if hasattr(q, "options") and q.options:
            random.shuffle(q.options)
    return questions


# store_session_data と get_session_data も同様にクライアントを依存性注入で受け取るように変更
async def store_session_data(
    session_dynamodb_table: Annotated[Any, Depends(get_session_data_table)],  # 変更
    session_id: str,
    problems: List[ProblemData],
) -> None:
    # ... (内部ロジックは変更なし)
    func_start_time = time.perf_counter()
    logger.info(
        f"store_session_data: START - Storing session for sessionId: {session_id}, problems_count: {len(problems)}"
    )
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
        await session_dynamodb_table.put_item(
            Item=item_to_store
        )  # session_dynamodb_table を直接使用
        func_end_time = time.perf_counter()
        logger.info(
            f"store_session_data: END - Session data stored for sessionId: {session_id} in {func_end_time - func_start_time:.4f} seconds."
        )
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
    session_dynamodb_table: Annotated[Any, Depends(get_session_data_table)],  # 変更
    session_id: str,
) -> Optional[SessionData]:
    # ... (内部ロジックは変更なし)
    func_start_time = time.perf_counter()
    logger.info(
        f"get_session_data: START - Retrieving session for sessionId: {session_id}"
    )
    try:
        response = await session_dynamodb_table.get_item(
            Key={"sessionId": session_id}
        )  # session_dynamodb_table を直接使用
        item = response.get("Item")
        if not item:
            logger.warning(f"Session data not found for sessionId: {session_id}")
            return None
        current_time = int(time.time())
        if "ttl" in item and item["ttl"] < current_time:
            logger.info(
                f"Session {session_id} has expired (TTL: {item['ttl']}, Current: {current_time})."
            )
            return None
        validated_data = SessionData.model_validate(item)
        func_end_time = time.perf_counter()
        logger.info(
            f"get_session_data: END - Session data retrieved and validated for {session_id} in {func_end_time - func_start_time:.4f} seconds."
        )
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
    # (変更なし)
    results = []
    correct_data_map = session_data.problem_data
    for user_ans in user_answers:
        q_id = user_ans.questionId
        if q_id not in correct_data_map:
            logger.warning(
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


# --- API Endpoints (クライアント取得方法を依存性注入に変更) ---
@app.get("/questions", response_model=QuestionResponse)
async def get_quiz_questions(
    # request: Request, # 不要
    bookSource: Annotated[
        Literal["readable_code", "programming_principles", "both"],
        Query(description="問題の出典"),
    ],
    count: Annotated[int, Query(ge=1, le=50, description="取得する問題数")],
    timeLimit: Annotated[
        int, Query(ge=10, le=300, description="1問あたりの制限時間(秒)")
    ],
    # 依存性注入でクライアントを取得
    dynamodb_client_injected: Annotated[Any, Depends(get_dynamodb_client)],
    quiz_problems_table_injected: Annotated[Any, Depends(get_quiz_problems_table)],
    session_data_table_injected: Annotated[Any, Depends(get_session_data_table)],
    request: Request,  # Requestオブジェクトはaws_request_id取得のために残す
):
    endpoint_start_time = time.perf_counter()
    aws_request_id = "N/A"
    if (
        "aws.lambda_context" in request.scope
    ):  # requestオブジェクトはaws_request_id取得のために必要
        aws_request_id = request.scope["aws.lambda_context"].aws_request_id
    logger.info(
        f"[ReqID: {aws_request_id}] /questions: START - bookSource='{bookSource}', count={count}, timeLimitPerQuestion={timeLimit}"
    )

    # クライアント存在確認ロジックは不要になる (Dependsが初期化を保証、失敗時はHTTPException)
    # logger.info(f"[ReqID: {aws_request_id}] /questions: AWS clients successfully retrieved via Depends.")

    try:
        step_start_time = time.perf_counter()
        problems_from_db = await get_questions_from_dynamodb(
            dynamodb_client_injected, quiz_problems_table_injected, bookSource, count
        )
        step_end_time = time.perf_counter()
        logger.info(
            f"[ReqID: {aws_request_id}] /questions: Step 'get_questions_from_dynamodb' completed in {step_end_time - step_start_time:.4f} seconds. Found {len(problems_from_db)} problems."
        )

        if not problems_from_db:
            logger.warning(
                f"[ReqID: {aws_request_id}] /questions: No problems returned from get_questions_from_dynamodb for source: {bookSource}"
            )
            raise ServiceError(
                status_code=404,
                detail=f"No questions could be loaded for source: {bookSource}. Please try a different source or smaller count.",
            )

        step_start_time = time.perf_counter()
        shuffled_problems = shuffle_options(problems_from_db)
        step_end_time = time.perf_counter()
        logger.info(
            f"[ReqID: {aws_request_id}] /questions: Step 'shuffle_options' completed in {step_end_time - step_start_time:.4f} seconds."
        )

        session_id = f"sess_{uuid.uuid4()}"
        step_start_time = time.perf_counter()
        await store_session_data(
            session_data_table_injected, session_id, shuffled_problems
        )
        step_end_time = time.perf_counter()
        logger.info(
            f"[ReqID: {aws_request_id}] /questions: Step 'store_session_data' completed in {step_end_time - step_start_time:.4f} seconds for sessionId: {session_id}."
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
        endpoint_end_time = time.perf_counter()
        logger.info(
            f"[ReqID: {aws_request_id}] /questions: END - Successfully processed request in {endpoint_end_time - endpoint_start_time:.4f} seconds. Returning {len(response_questions)} questions."
        )
        return final_response
    except ServiceError as e:
        endpoint_end_time = time.perf_counter()
        logger.error(
            f"[ReqID: {aws_request_id}] /questions: ServiceError caught at endpoint level after {endpoint_end_time - endpoint_start_time:.4f}s. Status: {e.status_code}, Detail: {e.detail}"
        )
        raise e
    except Exception as e:
        endpoint_end_time = time.perf_counter()
        logger.critical(
            f"[ReqID: {aws_request_id}] /questions: CRITICAL - Unexpected error in get_quiz_questions after {endpoint_end_time - endpoint_start_time:.4f}s: {e}",
            exc_info=True,
        )
        raise ServiceError(
            status_code=500,
            detail="An unexpected error occurred while processing your request.",
        )


@app.post("/answers", response_model=AnswerResponse)
async def submit_answers(
    # request: Request, # 不要
    answer_request: Annotated[AnswerRequest, Body(description="ユーザーの解答")],
    session_data_table_injected: Annotated[
        Any, Depends(get_session_data_table)
    ],  # 変更
    request: Request,  # Requestオブジェクトはaws_request_id取得のために残す
):
    endpoint_start_time = time.perf_counter()
    aws_request_id = "N/A"
    if (
        "aws.lambda_context" in request.scope
    ):  # requestオブジェクトはaws_request_id取得のために必要
        aws_request_id = request.scope["aws.lambda_context"].aws_request_id
    logger.info(
        f"[ReqID: {aws_request_id}] /answers: START - sessionId='{answer_request.sessionId}', num_answers={len(answer_request.answers)}"
    )

    # クライアント存在確認ロジックは不要になる
    # logger.info(f"[ReqID: {aws_request_id}] /answers: session_data_table successfully retrieved via Depends.")

    session_id = answer_request.sessionId
    user_answers = answer_request.answers

    try:
        step_start_time = time.perf_counter()
        session_data = await get_session_data(session_data_table_injected, session_id)
        step_end_time = time.perf_counter()
        logger.info(
            f"[ReqID: {aws_request_id}] /answers: Step 'get_session_data' completed in {step_end_time - step_start_time:.4f} seconds."
        )

        if session_data is None:
            logger.warning(
                f"[ReqID: {aws_request_id}] /answers: Session not found or expired for sessionId: {session_id}"
            )
            raise ServiceError(status_code=404, detail="Session not found or expired.")

        step_start_time = time.perf_counter()
        results = validate_answers(user_answers, session_data)
        step_end_time = time.perf_counter()
        logger.info(
            f"[ReqID: {aws_request_id}] /answers: Step 'validate_answers' completed in {step_end_time - step_start_time:.4f} seconds. Produced {len(results)} results."
        )

        response = AnswerResponse(results=results)
        endpoint_end_time = time.perf_counter()
        logger.info(
            f"[ReqID: {aws_request_id}] /answers: END - Successfully processed request in {endpoint_end_time - endpoint_start_time:.4f} seconds."
        )
        return response
    except ServiceError as e:
        endpoint_end_time = time.perf_counter()
        logger.error(
            f"[ReqID: {aws_request_id}] /answers: ServiceError caught at endpoint level after {endpoint_end_time - endpoint_start_time:.4f}s. Status: {e.status_code}, Detail: {e.detail}"
        )
        raise e
    except Exception as e:
        endpoint_end_time = time.perf_counter()
        logger.critical(
            f"[ReqID: {aws_request_id}] /answers: CRITICAL - Unexpected error in submit_answers after {endpoint_end_time - endpoint_start_time:.4f}s: {e}",
            exc_info=True,
        )
        raise ServiceError(
            status_code=500,
            detail="An unexpected error occurred while processing your answers.",
        )


@app.get("/")
async def root():
    logger.info("Root path '/' accessed.")
    return {"message": "Quiz App Backend is running!"}


# Mangumハンドラ - lifespan="off" が重要
handler = Mangum(app, lifespan="off")
