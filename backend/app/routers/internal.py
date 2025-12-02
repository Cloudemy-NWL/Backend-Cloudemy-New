from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field
from typing import List, Dict, Any
from datetime import datetime, timezone
import os

from app.db import get_db

router = APIRouter(prefix="/api/internal", tags=["internal"])

# Runner → Backend 콜백 보호용 토큰
RESULT_TOKEN = os.getenv("INTERNAL_RESULT_TOKEN", "secret")


# ====== Pydantic 모델 ======
class FeedbackItem(BaseModel):
    case: str
    message: str


class MetricsIn(BaseModel):
    timeMs: int = 0
    memoryMB: int = 0


class ResultIn(BaseModel):
    status: str
    score: float = 0.0
    fail_tags: List[str] = Field(default_factory=list)
    feedback: List[FeedbackItem] = Field(default_factory=list)
    metrics: MetricsIn = Field(default_factory=MetricsIn)


class OkOut(BaseModel):
    ok: bool = True
    submission_id: str
    status: str


def COLL():
    return get_db().submissions


# ====== 채점 결과 콜백(내부) API ======
@router.post(
    "/submissions/{submission_id}/result",
    response_model=OkOut,
    status_code=status.HTTP_200_OK,
)
async def post_result_callback(
    submission_id: str,
    payload: ResultIn,
    x_result_token: str = Header(None, alias="X-Result-Token"),
):
    # 0) 토큰 검증
    if not x_result_token or x_result_token != RESULT_TOKEN:
        print(f"[Internal] ❌ invalid result token. header={x_result_token}")
        raise HTTPException(status_code=401, detail="invalid result token")

    # 1) 기존 문서 조회
    doc = await COLL().find_one({"_id": submission_id})
    if not doc:
        print(f"[Internal] ❌ submission not found. submission_id={submission_id}")
        raise HTTPException(status_code=404, detail="submission not found")

    # 2) 이미 FINALIZED면 업데이트 안 하고 그대로 OK
    if doc.get("finalized") is True or doc.get("status") == "FINALIZED":
        print(f"[Internal] 이미 FINALIZED 상태. submission_id={submission_id}")
        return OkOut(ok=True, submission_id=submission_id, status=doc.get("status", "FINALIZED"))

    # 3) 상태 정규화 / 검증
    incoming = (payload.status or "").upper()

    # Runner가 SUCCESS / SUCCESSED 라고 보내도 COMPLETED로 통일
    if incoming in ("SUCCESS", "SUCCESSED"):
        incoming = "COMPLETED"

    allowed = {"COMPLETED", "FAILED", "TIMEOUT"}
    if incoming not in allowed:
        print(f"[Internal] ❌ invalid status: {incoming}")
        raise HTTPException(status_code=400, detail=f"invalid status: {incoming}")

    # 4) DB에 반영할 내용 구성 (status + score + fail_tags + feedback + metrics)
    update_doc: Dict[str, Any] = {
        "status": incoming,
        "score": float(payload.score or 0),
        "fail_tags": list(payload.fail_tags or []),
        "feedback": [fi.model_dump() for fi in (payload.feedback or [])],
        "metrics": payload.metrics.model_dump() if payload.metrics else {},
        "updated_at": datetime.now(timezone.utc),
    }

    # 5) FINALIZED와 경합 방지: finalized != True 인 것만 업데이트
    res = await COLL().update_one(
        {"_id": submission_id, "finalized": {"$ne": True}},
        {"$set": update_doc},
    )

    print(
        f"[Internal] result update. submission_id={submission_id}, "
        f"matched={res.matched_count}, modified={res.modified_count}, "
        f"update_doc={update_doc}"
    )

    # 경쟁 상황 등으로 matched가 0이면, 현재 상태를 다시 읽어서 반환
    if res.matched_count == 0:
        doc = await COLL().find_one({"_id": submission_id})
        return OkOut(ok=True, submission_id=submission_id, status=doc.get("status", incoming))

    return OkOut(ok=True, submission_id=submission_id, status=incoming)