#!/usr/bin/env python3
"""
원클릭 폭주쇼 - HPA 시연용 부하 테스트 스크립트
사용법: python load-test.py [BACKEND_URL] [REQUEST_COUNT] [CONCURRENT]
"""

import asyncio
import aiohttp
import sys
import time
from datetime import datetime

# 기본값
BACKEND_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
TOTAL_REQUESTS = int(sys.argv[2]) if len(sys.argv) > 2 else 100
CONCURRENT = int(sys.argv[3]) if len(sys.argv) > 3 else 20

# 테스트용 코드
TEST_CODE = 'print("Hello, World!")'

async def send_request(session, url, request_id):
    """단일 요청 전송"""
    payload = {
        "language": "python",
        "code": TEST_CODE
    }
    try:
        start_time = time.time()
        async with session.post(url, json=payload) as response:
            elapsed = time.time() - start_time
            status = response.status
            if status == 201:
                data = await response.json()
                return {
                    "success": True,
                    "status": status,
                    "elapsed": elapsed,
                    "submission_id": data.get("submission_id", ""),
                }
            else:
                return {
                    "success": False,
                    "status": status,
                    "elapsed": elapsed,
                }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "elapsed": 0,
        }

async def run_load_test():
    """부하 테스트 실행"""
    url = f"{BACKEND_URL}/submissions"
    
    print("🚀 원클릭 폭주쇼 시작!")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"📍 Backend URL: {BACKEND_URL}")
    print(f"📊 총 요청 수: {TOTAL_REQUESTS}")
    print(f"⚡ 동시 요청 수: {CONCURRENT}")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()
    
    start_time = time.time()
    success_count = 0
    error_count = 0
    total_elapsed = 0
    
    # 세마포어로 동시 요청 수 제한
    semaphore = asyncio.Semaphore(CONCURRENT)
    
    async def bounded_request(session, url, request_id):
        async with semaphore:
            return await send_request(session, url, request_id)
    
    async with aiohttp.ClientSession() as session:
        # 모든 요청 생성
        tasks = [
            bounded_request(session, url, i)
            for i in range(TOTAL_REQUESTS)
        ]
        
        # 진행 상황 표시
        print("🔥 부하 발생 중...")
        print()
        
        # 요청 실행 및 결과 수집
        results = await asyncio.gather(*tasks)
        
        # 결과 집계
        for result in results:
            if result.get("success"):
                success_count += 1
            else:
                error_count += 1
            total_elapsed += result.get("elapsed", 0)
    
    total_time = time.time() - start_time
    avg_elapsed = total_elapsed / TOTAL_REQUESTS if TOTAL_REQUESTS > 0 else 0
    rps = TOTAL_REQUESTS / total_time if total_time > 0 else 0
    
    # 결과 출력
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("📈 테스트 결과")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"✅ 성공: {success_count}")
    print(f"❌ 실패: {error_count}")
    print(f"⏱️  총 소요 시간: {total_time:.2f}초")
    print(f"📊 평균 응답 시간: {avg_elapsed*1000:.2f}ms")
    print(f"🚀 초당 요청 수 (RPS): {rps:.2f}")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()
    print("💡 다음 명령어로 HPA 상태 확인:")
    print("   kubectl get hpa backend-hpa -w")
    print("   kubectl get pods -l app=backend -w")

if __name__ == "__main__":
    asyncio.run(run_load_test())

