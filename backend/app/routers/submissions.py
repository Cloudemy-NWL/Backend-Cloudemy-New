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

print(f"[제출] 모듈 로드 완료. REDIS_URL={REDIS_URL}, QUEUE_NAME={QUEUE_NAME}")


# ====== Pydantic 모델 ======
class SubmissionCreate(BaseModel):
    language: constr(strip_whitespace=True, min_length=1) = "python"
    code: str

class FeedbackItem(BaseModel):
    case: str
    message: str

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

class SubmissionQueued(BaseModel):
    submission_id: str
    status: str = "QUEUED"
    attempt: int = 1
    created_at: datetime

class FinalizeIn(BaseModel):
    note: Optional[str] = None

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

async def _enqueue_to_queue(message: dict) -> None:
    print(f"[제출] Redis 큐에 작업 등록 시작: {message}")
    r = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        length = await r.lpush(QUEUE_NAME, json.dumps(message))
        print(f"[제출] Redis 큐 등록 완료. 현재 큐 길이={length}")
    except Exception as e:
        print(f"[제출] ❌ Redis 큐 등록 중 오류 발생: {e}")
        raise
    finally:
        await r.close()


async def _save_submission_to_redis(submission_id: str, payload: SubmissionCreate) -> None:
    print(f"[제출] Redis에 제출 데이터 저장 시작. submission_id={submission_id}")
    r = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        key = f"submission:{submission_id}"
        mapping = {
            "submission_id": submission_id,
            "user_id": "u1",
            "language": payload.language,
            "code": payload.code,
        }
        await r.hset(key, mapping=mapping)
        print(f"[제출] Redis 저장 완료. key={key}, 필드={list(mapping.keys())}")
    except Exception as e:
        print(f"[제출] ❌ Redis 저장 중 오류 발생: {e}")
        raise
    finally:
        await r.close()


def COLL():
    return get_db().submissions


async def _get_doc_or_404(submission_id: str) -> dict:
    print(f"[제출] 제출 조회 시작. submission_id={submission_id}")
    doc = await COLL().find_one({"_id": submission_id})
    if not doc:
        print(f"[제출] ❌ 제출 데이터 없음. submission_id={submission_id}")
        raise HTTPException(status_code=404, detail="submission not found")
    return doc


def _doc_to_out(doc: dict) -> SubmissionOut:
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


# ====== (1) 코드 제출 ======
@router.post("", response_model=SubmissionQueued, status_code=status.HTTP_201_CREATED)
async def create_submission(payload: SubmissionCreate):
    print(f"[제출] 코드 제출 요청 수신. 언어={payload.language}, 코드 길이={len(payload.code)}")

    now = datetime.now(timezone.utc)
    submission_id = str(ObjectId())
    print(f"[제출] 신규 submission_id 생성: {submission_id}")

    doc = {
        "_id": submission_id,
        "user_id": "u1",
        "language": payload.language,
        "code": payload.code,
        "status": "QUEUED",
        "score": 0,
        "fail_tags": [],
        "feedback": [],
        "metrics": Metrics().model_dump(),
        "finalized": False,
        "attempt": 1,
        "created_at": now,
    }

    await COLL().insert_one(doc)
    print(f"[제출] MongoDB 저장 완료. submission_id={submission_id}")

    await _save_submission_to_redis(submission_id, payload)

    await _enqueue_to_queue({
        "submission_id": submission_id,
        "language": payload.language,
    })

    print(f"[제출] ✅ 제출 큐 등록 완료. submission_id={submission_id}")

    return SubmissionQueued(
        submission_id=submission_id,
        status="QUEUED",
        attempt=1,
        created_at=now
    )


# ====== (3) 결과 조회 ======
@router.get("/{submission_id}", response_model=SubmissionOut)
async def get_submission(submission_id: str):
    print(f"[제출] 결과 조회 요청. submission_id={submission_id}")
    doc = await _get_doc_or_404(submission_id)
    print(f"[제출] 조회 성공. 현재 상태={doc.get('status')}")
    return _doc_to_out(doc)


# ====== (4) 최종 제출 ======
@router.post("/{submission_id}/finalize", response_model=FinalizeOut)
async def finalize_submission(submission_id: str, body: FinalizeIn):
    print(f"[제출] 최종 제출 요청. submission_id={submission_id}, 메모={body.note}")
    doc = await _get_doc_or_404(submission_id)
    user_id = doc.get("user_id")

    if doc.get("finalized"):
        print(f"[제출] 이미 최종 제출된 데이터입니다.")
        return FinalizeOut(submission_id=submission_id)

    existing = await COLL().find_one({
        "user_id": user_id,
        "finalized": True
    })

    if existing and existing.get("_id") != submission_id:
        print(f"[제출] ❌ 이미 다른 최종 제출이 존재합니다.")
        raise HTTPException(status_code=409, detail="finalized_submission_exists_for_user")

    res = await COLL().update_one(
        {"_id": submission_id, "finalized": {"$ne": True}},
        {"$set": {
            "status": "FINALIZED",
            "finalized": True,
            "finalize_note": body.note
        }}
    )

    print(f"[제출] 최종 제출 처리 완료. matched={res.matched_count}, modified={res.modified_count}")

    if res.matched_count == 0:
        doc = await _get_doc_or_404(submission_id)
        if not doc.get("finalized"):
            print(f"[제출] ❌ 최종 제출 충돌 발생")
            raise HTTPException(status_code=409, detail="finalize_conflict")

    return FinalizeOut(submission_id=submission_id)


# ====== (5) 리스트 조회 ======
@router.get("", response_model=SubmissionListOut)
async def list_submissions(
    submission_id: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=100),
):
    print(f"[제출] 리스트 조회 요청. submission_id={submission_id}, status={status}, page={page}, size={size}")

    q: dict = {}
    if submission_id:
        q["_id"] = submission_id
    if status:
        q["status"] = status

    total = await COLL().count_documents(q)
    print(f"[제출] 조회 대상 총 개수={total}")

    cursor = (
        COLL().find(q, projection={"code": 0, "feedback": 0, "metrics": 0})
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

    print(f"[제출] 리스트 조회 완료. 반환 개수={len(items)}")
    return SubmissionListOut(items=items, total=total, page=page, size=size)
