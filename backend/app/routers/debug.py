# app/routers/debug.py
from fastapi import APIRouter
import time
import math

router = APIRouter(prefix="/api/debug", tags=["debug"])

@router.get("/cpu-burn")
def cpu_burn(seconds: int = 5):
    """
    HPA 테스트용 CPU 부하 엔드포인트.
    seconds 동안 CPU를 바쁘게 돌린다.
    """
    start = time.time()
    end = start + seconds

    x = 0.0
    while time.time() < end:
        # CPU를 일부러 사용하기 위한 의미 없는 연산
        x += math.sqrt(12345.6789)

    elapsed = time.time() - start
    return {
        "status": "ok",
        "target_seconds": seconds,
        "elapsed": elapsed,
        "dummy": x,  # 최적화 방지용
    }