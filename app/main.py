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
_aws_dynamodb_client: Optional[Any] = None
_aws_quiz_problems_table: Optional[Any] = None
_aws_session_data_table: Optional[Any] = None
_client_init_lock = asyncio.Lock()


async def _initialize_global_aws_clients():
    global _aws_dynamodb_client, _aws_quiz_problems_table, _aws_session_data_table
    async with _client_init_lock:
        if _aws_dynamodb_client is None:
            logger.info("Initializing AWS clients globally...")
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
            logger.info("AWS clients initialized globally.")
        else:
            logger.info("AWS clients already initialized globally.")


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


app = FastAPI(title="Quiz App Backend")

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


# --- DynamoDB Operations Helpers ---


async def _get_all_question_ids_for_source(
    quiz_problems_table: Any, book_source_value: str
) -> List[str]:
    """
    指定されたbookSourceのすべての問題IDをページネーションを使用して取得する。
    """
    question_ids: List[str] = []
    last_evaluated_key: Optional[Dict[str, Any]] = None
    query_count = 0  # 念のため無限ループを防ぐカウンター（本番では調整または削除）

    logger.info(
        f"Fetching all question IDs for bookSource: {book_source_value} using GSI: {settings.gsi_book_source_index_name}"
    )
    while (
        query_count < 10
    ):  # 最大10ページまで（DynamoDBの1MB制限とアイテムサイズによる）
        # この制限は実際のデータ量に応じて調整が必要
        query_kwargs = {
            "IndexName": settings.gsi_book_source_index_name,
            "KeyConditionExpression": Key("bookSource").eq(book_source_value),
            "ProjectionExpression": "questionId",  # 取得する属性をquestionIdのみに限定
        }
        if last_evaluated_key:
            query_kwargs["ExclusiveStartKey"] = last_evaluated_key

        try:
            response = await quiz_problems_table.query(**query_kwargs)
            query_count += 1
        except ClientError as e:
            logger.error(
                f"ClientError during GSI query for {book_source_value} (attempt {query_count}): {e}",
                exc_info=True,
            )
            # リトライ処理を挟むか、エラーとして上位に投げるか検討。ここでは投げる。
            raise ServiceError(
                status_code=500,
                detail=f"Database query failed for source '{book_source_value}'.",
            )

        items = response.get("Items", [])
        for item in items:
            if "questionId" in item:
                question_ids.append(item["questionId"])

        last_evaluated_key = response.get("LastEvaluatedKey")
        if not last_evaluated_key:
            logger.info(
                f"Finished fetching all question IDs for {book_source_value}. Total IDs: {len(question_ids)} after {query_count} queries."
            )
            break
        else:
            logger.info(
                f"Paginating for {book_source_value}. So far {len(question_ids)} IDs after {query_count} queries. Next key: {last_evaluated_key}"
            )

    if last_evaluated_key:  # ループが最大試行回数で終了した場合
        logger.warning(
            f"Stopped fetching question IDs for {book_source_value} due to query limit ({query_count} queries). "
            f"Some questions might be missing if there were more pages. LastEvaluatedKey was: {last_evaluated_key}"
        )

    return question_ids


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
        tasks_for_ids_collection = []
        for src in target_book_sources:
            # 各ソースの全問題ID取得をタスクとして追加
            tasks_for_ids_collection.append(
                _get_all_question_ids_for_source(quiz_problems_table, src)
            )

        # asyncio.gather を使って各ソースのIDリストを並行して取得
        results_per_source = await asyncio.gather(
            *tasks_for_ids_collection, return_exceptions=True
        )

        for idx, source_result in enumerate(results_per_source):
            current_source = target_book_sources[idx]
            if isinstance(source_result, Exception):
                # _get_all_question_ids_for_source内でServiceErrorが投げられるか、ClientErrorがここで捕捉される
                if isinstance(source_result, ServiceError):
                    raise source_result  # そのまま投げる
                logger.error(
                    f"Failed to fetch all question IDs for '{current_source}': {source_result}",
                    exc_info=True,
                )
                # ここで ServiceError を発生させる
                raise ServiceError(
                    status_code=500,
                    detail=f"Could not retrieve complete question ID list for '{current_source}'.",
                )
            # source_result は question_ids のリスト
            all_question_ids_from_gsi.extend(source_result)
            # logger.info(f"Successfully fetched {len(source_result)} question IDs for bookSource: {current_source}") # _get_all_question_ids_for_source内でログ出力済

    except ClientError as e:  # _get_all_question_ids_for_source で捕捉されなかった場合や、gather起因のClientError
        logger.error(
            f"DynamoDB ClientError while gathering all question IDs: {e}", exc_info=True
        )
        raise ServiceError(
            status_code=500,
            detail="Error communicating with database for complete question IDs.",
        )
    except ServiceError:  # 上記の raise source_result や raise ServiceError を再throw
        raise
    except Exception as e:  # その他の予期せぬエラー
        logger.error(
            f"Unexpected error while fetching all question IDs: {e}", exc_info=True
        )
        raise ServiceError(
            status_code=500, detail="Unexpected error fetching complete question IDs."
        )

    # GSIクエリ後のログからページネーション未対応の警告は削除
    # logger.warning(f"Warning: More items available for bookSource {current_source}...") は不要

    if not all_question_ids_from_gsi:
        raise ServiceError(
            status_code=404,
            detail=f"No questions found for source(s): {', '.join(target_book_sources)}",
        )

    # 重複するIDがある場合、一意にする（"both" の場合に理論上ありえるが、現状のデータ構造では問題IDはユニークのはず）
    # ただし、異なるソースで同じ問題IDが使われる可能性が将来的にあるなら考慮
    unique_question_ids = list(set(all_question_ids_from_gsi))
    if len(unique_question_ids) != len(all_question_ids_from_gsi):
        logger.info(
            f"Duplicate question IDs found and removed. Original count: {len(all_question_ids_from_gsi)}, Unique count: {len(unique_question_ids)}"
        )

    all_question_ids_from_gsi = unique_question_ids

    num_to_fetch = min(count, len(all_question_ids_from_gsi))
    if num_to_fetch < count:
        logger.warning(
            f"Requested {count} questions, but only {len(all_question_ids_from_gsi)} unique questions available for source(s) '{', '.join(target_book_sources)}'. Fetching {num_to_fetch}."
        )

    if num_to_fetch == 0:
        return []

    selected_ids = random.sample(all_question_ids_from_gsi, num_to_fetch)
    logger.info(
        f"Selected {len(selected_ids)} random question IDs for BatchGetItem from a pool of {len(all_question_ids_from_gsi)} unique IDs."
    )

    problems: List[ProblemData] = []
    if not selected_ids:
        return problems

    try:
        keys_for_batch_get = [{"questionId": {"S": q_id}} for q_id in selected_ids]
        # BatchGetItemは最大100アイテムまで。selected_idsが100を超える場合は分割が必要。
        # 現状のcountのmaxは50なので、selected_idsが100を超えることはない。
        request_items = {
            settings.dynamodb_quiz_problems_table_name: {
                "Keys": keys_for_batch_get,
                # "ConsistentRead": True # 必要に応じて整合性を高めるが、コストとレイテンシに影響
            }
        }
        response = await dynamodb_client.batch_get_item(RequestItems=request_items)

        raw_problems_from_db = response.get("Responses", {}).get(
            settings.dynamodb_quiz_problems_table_name, []
        )
        logger.info(f"Retrieved {len(raw_problems_from_db)} items from BatchGetItem.")

        if response.get("UnprocessedKeys", {}).get(
            settings.dynamodb_quiz_problems_table_name
        ):
            # UnprocessedKeysの処理は複雑なので、ここでは警告に留める。
            # 本番環境ではリトライロジックを検討。
            logger.warning(
                "Warning: BatchGetItem returned UnprocessedKeys, some items may not have been retrieved. "
                f"Unprocessed count: {len(response['UnprocessedKeys'][settings.dynamodb_quiz_problems_table_name]['Keys'])}"
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
            logger.error(
                f"Error validating problem data from DynamoDB: {e}, item_id: {item_dict.get('questionId', {}).get('S')}",
                exc_info=True,
            )
            continue

    if (
        not problems and num_to_fetch > 0
    ):  # 取得IDはあったが、BatchGetItemやパースで全滅した場合
        logger.error(
            "Failed to load or validate any question data after fetching and BatchGetItem, though IDs were selected."
        )
        raise ServiceError(
            status_code=500,
            detail="Failed to load or validate any question data after fetching.",
        )

    logger.info(
        f"Successfully parsed and validated {parsed_count} problems out of {len(raw_problems_from_db)} raw items from DB."
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
            logger.warning(f"Session data not found for sessionId: {session_id}")
            return None
        current_time = int(time.time())
        if "ttl" in item and item["ttl"] < current_time:
            logger.info(
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
    logger.info(
        f"[ReqID: {aws_request_id}] GET /questions: START - bookSource='{bookSource}', count={count}, timeLimitPerQuestion={timeLimit}"
    )

    try:
        problems_from_db = await get_questions_from_dynamodb(
            dynamodb_client_injected, quiz_problems_table_injected, bookSource, count
        )

        if (
            not problems_from_db
        ):  # get_questions_from_dynamodb が空リストを返し、かつエラーでない場合
            logger.warning(
                f"[ReqID: {aws_request_id}] /questions: No problems could be loaded or selected for source: {bookSource}, though no direct error was raised by get_questions_from_dynamodb. This might happen if count is 0 or no IDs were sampled."
            )
            # get_questions_from_dynamodb内で適切なServiceError(404 or 500)がスローされるはずなので、ここでの404は限定的
            # ただし、num_to_fetch が0の場合、空リストが返るので、それを考慮
            if count > 0:  # count > 0 なのに問題が0件は問題あり
                raise ServiceError(
                    status_code=404,  # 問題が見つからなかったケース
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
            timeLimit=timeLimit
            * len(response_questions),  # 問題数が0の場合、timeLimitも0になる
            sessionId=session_id,
        )
        logger.info(
            f"[ReqID: {aws_request_id}] GET /questions: END - Successfully processed. Returning {len(response_questions)} questions."
        )
        return final_response
    except ServiceError as e:
        logger.error(
            f"[ReqID: {aws_request_id}] GET /questions: ServiceError. Status: {e.status_code}, Detail: {e.detail}"
        )
        raise e
    except Exception as e:
        logger.critical(
            f"[ReqID: {aws_request_id}] GET /questions: CRITICAL - Unexpected error: {e}",
            exc_info=True,
        )
        raise ServiceError(
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
    logger.info(
        f"[ReqID: {aws_request_id}] POST /answers: START - sessionId='{answer_request.sessionId}', num_answers={len(answer_request.answers)}"
    )

    session_id = answer_request.sessionId
    user_answers = answer_request.answers

    try:
        session_data = await get_session_data(session_data_table_injected, session_id)

        if session_data is None:
            raise ServiceError(status_code=404, detail="Session not found or expired.")

        results = validate_answers(user_answers, session_data)
        response = AnswerResponse(results=results)
        logger.info(
            f"[ReqID: {aws_request_id}] POST /answers: END - Successfully processed."
        )
        return response
    except ServiceError as e:
        logger.error(
            f"[ReqID: {aws_request_id}] POST /answers: ServiceError. Status: {e.status_code}, Detail: {e.detail}"
        )
        raise e
    except Exception as e:
        logger.critical(
            f"[ReqID: {aws_request_id}] POST /answers: CRITICAL - Unexpected error: {e}",
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


handler = Mangum(app, lifespan="off")
