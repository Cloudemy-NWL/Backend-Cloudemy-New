from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field, conlist
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
import os

from app.db import get_db

router = APIRouter(prefix="/internal", tags=["internal"])

# 보안 토큰 (Runner → Backend 콜백 보호)
RESULT_TOKEN = os.getenv("INTERNAL_RESULT_TOKEN", "secret")


class FeedbackItem(BaseModel):
    case: str
    message: str


class ResultIn(BaseModel):
    status: str
    fail_tags: List[str] = Field(default_factory=list)
    feedback: List[FeedbackItem] = Field(default_factory=list)


class OkOut(BaseModel):
    ok: bool = True
    submission_id: str
    status: str

def COLL():
    return get_db().submissions


# 채점 결과 콜백(내부) API
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
        raise HTTPException(status_code=401, detail="invalid result token")

    # 1) 문서 조회
    doc = await COLL().find_one({"_id": submission_id})
    if not doc:
        raise HTTPException(status_code=404, detail="submission not found")

    # 2) 이미 FINALIZED이면 무시 (idempotent OK)
    if doc.get("finalized") is True or doc.get("status") == "FINALIZED":
        return OkOut(ok=True, submission_id=submission_id, status=doc.get("status", "FINALIZED"))
    
    # 3) 상태 정규화 / 검증
    incoming = (payload.status or ""). upper()

    if incoming in ("SUCCESS", "SUCCESSED"):
        incoming = "COMPLETED"
    
    allowed = {"COMPLETED", "FAILED", "TIMEOUT"}
    if incoming not in allowed:
        raise HTTPException(status_code=400, detail=f"invalid status: {incoming}")

    # 4) 업데이트 문서 
    update_doc: Dict[str, Any] = {
        "status": incoming,
        "fail_tags": list(payload.fail_tags or []), 
        "feedback": [fi.model_dump() for fi in (payload.feedback or [])], 
    }

    # 5) finalize 와 경합 방지: finalized != True 조건
    res = await COLL().update_one(
        {"_id": submission_id, "finalized": {"$ne": True}}, 
        {"$set": update_doc}, 
    )

    # 경쟁으로 매칭이 0일 수 있음 → 현재 상태 반환
    if res.matched_count == 0:
        doc = await COLL().find_one({"_id": submission_id})
        return OkOut(ok=True, submission_id=submission_id, status=doc.get("status", incoming))

    return OkOut(ok=True, submission_id=submission_id, status=incoming)