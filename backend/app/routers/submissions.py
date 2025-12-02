from fastapi import APIRouter, HTTPException, status, Query
from pydantic import BaseModel, Field, constr
from typing import List, Optional
from datetime import datetime, timezone
from bson import ObjectId
import os, json

# Redis (async)
from redis.asyncio import Redis

# Mongo (전역 연결)
from app.db import get_db

router = APIRouter(prefix="/api/submissions", tags=["submissions"])

# ====== 공통 ======
STATUSES = {"QUEUED", "FAILED", "COMPLETED", "TIMEOUT", "FINALIZED"}
QUEUE_NAME = os.getenv("QUEUE_SUBMISSIONS", "queue:submissions")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")

print(f"[Submissions] module loaded. REDIS_URL={REDIS_URL}, QUEUE_NAME={QUEUE_NAME}")  # [LOG ADDED]

# ====== Pydantic 모델 ======
# 클라이언트가 "코드 제출" 시 보내는 요청(request) 데이터 구조
class SubmissionCreate(BaseModel):
    language: constr(strip_whitespace=True, min_length=1) = "python"
    code: str

# 채점기(Runner)가 "케이스별 피드백"을 보낼 때 사용하는 구조 
class FeedbackItem(BaseModel):
    case: str
    message: str


# 서버가 "제출 결과"를 응답할 때 사용하는 전체 구조
class Metrics(BaseModel):
    timeMs: int = 0
    memoryMB: int = 0


class SubmissionOut(BaseModel):
    submission_id: str
    user_id: Optional[str] = None
    language: str = "python"
    status: constr(pattern="^(QUEUED|FAILED|COMPLETED|TIMEOUT|FINALIZED|SUCCESSED)$")
    score: float = 0
    fail_tags: List[str] = Field(default_factory=list)
    feedback: List[FeedbackItem] = Field(default_factory=list)
    metrics: Metrics = Field(default_factory=Metrics)
    finalized: bool = False
    created_at: datetime

# 학생이 코드 제출한 직후(POST /submissions) FastAPI가 응답으로 주는 모델 (큐 등록 상태 표현) 
class SubmissionQueued(BaseModel):
    submission_id: str
    status: str = "QUEUED"
    attempt: int = 1
    created_at: datetime

# 학생이 "최종 제출" 버튼을 눌렀을 때 보내는 요청(request) 구조 
class FinalizeIn(BaseModel):
    note: Optional[str] = None

# 서버가 최종 제출 확정 후 응답(response)으로 반환하는 구조 
class FinalizeOut(BaseModel):
    submission_id: str
    status: str = "FINALIZED"
    finalized: bool = True


class SubmissionListItem(BaseModel):
    submission_id: str
    language: str
    status: str
    score: float = 0
    created_at: Optional[datetime] = None

class SubmissionListOut(BaseModel):
    items: List[SubmissionListItem]
    total: int
    page: int
    size: int

# ====== 헬퍼 ======

# Redis 큐 등록 함수 
async def _enqueue_to_queue(message: dict) -> None:
    print(f"[Submissions] _enqueue_to_queue() called. message={message}")  # [LOG ADDED]
    r = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        length = await r.lpush(QUEUE_NAME, json.dumps(message))  # [LOG ADDED] (length 받기 위해 변수)
        print(f"[Submissions] LPUSH to queue '{QUEUE_NAME}'. new_length={length}")  # [LOG ADDED]
    except Exception as e:  # [LOG ADDED]
        print(f"[Submissions] ERROR while LPUSH to Redis: {e}")  # [LOG ADDED]
        raise
    finally:
        await r.close()


# Redis에 실제 코드/언어 저장 (runner가 나중에 꺼내 씀)
async def _save_submission_to_redis(submission_id: str, payload: SubmissionCreate) -> None:
    print(f"[Submissions] _save_submission_to_redis() called. submission_id={submission_id}")  # [LOG ADDED]
    r = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        key = f"submission:{submission_id}"  # [LOG ADDED]
        mapping = {  # [LOG ADDED]
            "submission_id": submission_id, 
            "user_id": "u1",
            "language": payload.language, 
            "code": payload.code, 
        }
        await r.hset(
            key,
            mapping=mapping,
        )
        print(f"[Submissions] HSET to key='{key}'. fields={list(mapping.keys())}")  # [LOG ADDED]
    except Exception as e:  # [LOG ADDED]
        print(f"[Submissions] ERROR while HSET to Redis: {e}")  # [LOG ADDED]
        raise
    finally:
        await r.close()

# db.submissions를 매번 쓰지 않고 COLL()로 접근하게 함
def COLL():
    return get_db().submissions


# 특정 제출 ID로 MongoDB에서 문서를 찾는 함수 (문서에 없으면 404 에러)
async def _get_doc_or_404(submission_id: str) -> dict:
    print(f"[Submissions] _get_doc_or_404() called. submission_id={submission_id}")  # [LOG ADDED]
    doc = await COLL().find_one({"_id": submission_id})
    if not doc:
        print(f"[Submissions] _get_doc_or_404() NOT FOUND: {submission_id}")  # [LOG ADDED]
        raise HTTPException(status_code=404, detail="submission not found")
    return doc


# MongoDB 문서를 SubmissionOut 응답 모델(Pydantic 객체)로 변환 
def _doc_to_out(doc: dict) -> SubmissionOut:
    # 너무 자주 호출될 수 있어서 여기선 자세한 로그는 생략 (필요하면 추가 가능)  # [LOG ADDED - comment only]
    return SubmissionOut(
        submission_id=doc["_id"],
        user_id=doc.get("user_id"),
        language=doc.get("language", "python"),
        status=doc.get("status", "QUEUED"),
        score=float(doc.get("score", 0) or 0),
        fail_tags=list(doc.get("fail_tags", [])),
        feedback=[FeedbackItem(**x) for x in doc.get("feedback", [])],
        metrics=Metrics(**(doc.get("metrics") or {})),
        finalized=bool(doc.get("finalized", False)),
        created_at=doc.get("created_at"),
    )



# ====== (1) 코드 제출: POST /submissions ======
@router.post(
    "",
    response_model=SubmissionQueued,
    status_code=status.HTTP_201_CREATED,
)
async def create_submission(payload: SubmissionCreate):
    print(f"[Submissions] create_submission() called. language={payload.language}, code_len={len(payload.code)}")  # [LOG ADDED]

    now = datetime.now(timezone.utc)
    print(f"[Submissions] current_time(UTC)={now.isoformat()}")  # [LOG ADDED]

    # 1) DB 저장 (status=QUEUED)
    submission_id = str(ObjectId())
    print(f"[Submissions] new submission_id={submission_id}")  # [LOG ADDED]

    doc = {
        "_id": submission_id,
        "user_id": "u1",  # 데모/시연: 하드코딩 사용자
        "language": payload.language,
        "code": payload.code,
        "status": "QUEUED",
        "score": 0,
        "fail_tags": [],
        "feedback": [],
        "metrics": Metrics().model_dump(),
        "finalized": False,
        "attempt": 1,  # 시연 고정
        "created_at": now, 
    }

    # 2) 비동기 DB 삽입 명령 
    await COLL().insert_one(doc)
    print(f"[Submissions] Mongo insert_one done. _id={submission_id}")  # [LOG ADDED]

    # 3) Redis 해시에 코드 저장 (runner가 읽어감)
    await _save_submission_to_redis(submission_id, payload)

    # 4) Redis 큐에 작업 push (scheduler가 감지)
    await _enqueue_to_queue({
        "submission_id": submission_id,
        "language": payload.language,
    })

    print(f"[Submissions] submission queued successfully. submission_id={submission_id}")  # [LOG ADDED]

    # 5) 응답
    return SubmissionQueued(
        submission_id=submission_id, 
        status="QUEUED",
        attempt=1,
        created_at=now
    )


# ====== (3) 코드 실행 결과 조회: GET /submissions/{id} ======
@router.get("/{submission_id}", response_model=SubmissionOut)
async def get_submission(submission_id: str):
    print(f"[Submissions] get_submission() called. submission_id={submission_id}")  # [LOG ADDED]
    doc = await _get_doc_or_404(submission_id)
    print(f"[Submissions] get_submission() found doc. status={doc.get('status')}")  # [LOG ADDED]
    return _doc_to_out(doc)


# ====== (4) 최종 제출 확정: POST /submissions/{id}/finalize ======
@router.post("/{submission_id}/finalize", response_model=FinalizeOut)
async def finalize_submission(submission_id: str, body: FinalizeIn):
    print(f"[Submissions] finalize_submission() called. submission_id={submission_id}, note={body.note}")  # [LOG ADDED]
    # 1) 현재 제출 찾기
    doc = await _get_doc_or_404(submission_id)
    user_id = doc.get("user_id")
    print(f"[Submissions] finalize_submission() loaded doc. user_id={user_id}, current_status={doc.get('status')}, finalized={doc.get('finalized')}")  # [LOG ADDED]

    # 2) 이미 최종화된 같은 제출이면 그대로 OK(idempotent)
    if doc.get("finalized"):
        print(f"[Submissions] finalize_submission() already finalized. submission_id={submission_id}")  # [LOG ADDED]
        return FinalizeOut(submission_id=submission_id)

    # 3) 같은 user_id로 이미 최종 제출이 있는지 확인
    existing = await COLL().find_one({
        "user_id": user_id,
        "finalized": True
    })
    # 이미 최종 제출이 있고, 그게 이번 제출이 아니라면 차단
    if existing and existing.get("_id") != submission_id:
        print(f"[Submissions] finalize_submission() conflict: other finalized submission exists. existing_id={existing.get('_id')}")  # [LOG ADDED]
        raise HTTPException(
            status_code=409,
            detail="finalized_submission_exists_for_user"
        )

    # 4) 현재 제출을 FINALIZED로 업데이트
    res = await COLL().update_one(
        {"_id": submission_id, "finalized": {"$ne": True}},
        {"$set": {
            "status": "FINALIZED",
            "finalized": True,
            "finalize_note": body.note
        }}
    )
    print(f"[Submissions] finalize_submission() update_one result: matched={res.matched_count}, modified={res.modified_count}")  # [LOG ADDED]

    # 경합으로 동시에 들어온 경우 방어
    if res.matched_count == 0:
        # 다시 조회해서 최종화 여부 확인
        doc = await _get_doc_or_404(submission_id)
        if not doc.get("finalized"):
            print(f"[Submissions] finalize_submission() finalize_conflict after update. submission_id={submission_id}")  # [LOG ADDED]
            raise HTTPException(status_code=409, detail="finalize_conflict")

    print(f"[Submissions] finalize_submission() done. submission_id={submission_id}")  # [LOG ADDED]
    return FinalizeOut(submission_id=submission_id)


# ====== (5) 리스트조회: GET /submissions ======
@router.get("", response_model=SubmissionListOut)
async def list_submissions(
    submission_id: Optional[str] = None,
    status: Optional[str] = None,  # "QUEUED|FAILED|COMPLETED|TIMEOUT|FINALIZED"
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=100),
):
    print(f"[Submissions] list_submissions() called. submission_id={submission_id}, status={status}, page={page}, size={size}")  # [LOG ADDED]

    # 검색 조건 (Query)
    q: dict = {}
    if submission_id:
        q["_id"] = submission_id
    if status:
        q["status"] = status

    print(f"[Submissions] list_submissions() query={q}")  # [LOG ADDED]

    coll = COLL()
    total = await coll.count_documents(q)
    print(f"[Submissions] list_submissions() total_matches={total}")  # [LOG ADDED]

    # DB에서 query에 맞는 문서 목록 조회 + 무거운 필드 제외 
    cursor = (
        coll.find(
            q,
            projection={
                "code": 0,
                "feedback": 0,
                "metrics": 0,
            },
        )
        .sort("created_at", -1)
        .skip((page - 1) * size)
        .limit(size)
    )

    docs = [d async for d in cursor]

    items = [
        SubmissionListItem(
            submission_id=d["_id"],
            language=d.get("language", "python"),
            status=d.get("status", "QUEUED"),
            score=float(d.get("score", 0) or 0),
            created_at=d.get("created_at"),
        )
        for d in docs
    ]

    print(f"[Submissions] list_submissions() done. returned_count={len(items)}")  # [LOG ADDED]
    return SubmissionListOut(items=items, total=total, page=page, size=size)
