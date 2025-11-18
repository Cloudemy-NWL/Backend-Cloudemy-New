import os
import json
import time
import signal
from datetime import datetime, timezone
from typing import Any, Dict

from redis import Redis
import requests
from openai import OpenAI

# 환경 변수
SUBMISSION_ID = os.getenv("SUBMISSION_ID")  # Scheduler가 Job 만들 때 넣어주는 값
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
BACKEND_INTERNAL_URL = os.getenv("BACKEND_INTERNAL_URL", "http://backend:8000/internal")
RESULT_TOKEN = os.getenv("INTERNAL_RESULT_TOKEN", "secret")

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

client = OpenAI(api_key=LLM_API_KEY)


# Redis에서 제출 데이터 로드
def load_submission_from_redis(submission_id: str) -> Dict[str, Any]:
    r = Redis.from_url(REDIS_URL, decode_responses=True)
    try: 
        key = f"submission:{submission_id}"
        data = r.hgetall(key)
        if not data:
            raise RuntimeError(f"Redis에서 데이터를 찾을 수 없습니다: {key}")
        return data
    finally:
        r.close()


# LLM에게 넘길 프롬프트 생성 함수
def build_prompt(code: str, language: str = "python") -> str:
    return f"""
너는 프로그래밍 자동 과제 채점기 역할을 하는 AI 야.

- 언어: {language}
- 학생이 제출한 코드는 아래와 같아.

```{language}
{code}

```

채점 기준은 다음과 같아:
1. 문법 에러가 있는지
2. 기본 요구사항(입출력 형식, 함수/변수 이름 등)을 지켰는지
3. 논리적으로 큰 버그가 있는지

아래 형식의 JSON만 출력해줘:

{{
    "status": "COMPLETED" 또는 "FAILED"
    "score": 0에서 100 사이의 숫자,
    "fail_tags": ["syntax_error", "logic_error", "requirement_miss"], 
    "feedback": [
    {{"case": "요약 키워드", "message": "학생에게 줄 피드백"}}
    ]
}}

JSON 외의 텍스트는 절대 출력하지 마.
"""


# LLM 호출
def call_llm(prompt:str) -> Dict[str, Any]:

    # 1) LLM 호출 
    response = client.responses.create(
        model=LLM_MODEL, 
        input=prompt, 
        max_output_tokens=500
    )

    # 2) 모델이 출력한 텍스트 가져오기 
    text = response.output[0].content[0].text

    # 3) json 파싱
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"JSON parsing failed: {e}\n--- LLM Output ---\n{text}"
        ) from e

    # 4) 필수 필드 검증
    for field in ("status", "score", "fail_tags", "feedback"):
        if field not in data:
            raise RuntimeError(
                f"Mission field in LLM result: {field}\n--- LLM Output ---\n{text}"
            )

    return data


# 내부 FastAPI에 결과를 POST 하는 함수
def send_result_to_backend(
    submission_id: str, 
    result: Dict[str, Any], 
    elapsed_ms: int,
    max_retries: int = 2,  # 총 2번 시도 (첫 시도 1번 + 재시도 1번)
) -> None:

    url = f"{BACKEND_INTERNAL_URL}/submissions/{submission_id}/result"

    payload = {
        "status": result["status"], 
        "score": float(result["score"]), 
        "fail_tags": result.get("fail_tags", []), 
        "feedback": result.get("feedback", []), 
        "metrics": {
            "timeMs": elapsed_ms, 
            "memoryMB": 0,
        }, 
    }

    last_error = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                url, 
                json=payload, 
                headers={"X-Result-Token": RESULT_TOKEN}, 
                timeout=10, 
            )
            
            if resp.ok:
                return  # 성공
            
            last_error = f"HTTP {resp.status_code}: {resp.text}"
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 2  # 2초, 4초, 6초...
                print(f"[Runner] Backend callback failed (attempt {attempt + 1}/{max_retries}), retrying in {wait_time}s: {last_error}")
                time.sleep(wait_time)
        
        except requests.exceptions.RequestException as e:
            last_error = str(e)
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 2
                print(f"[Runner] Backend callback error (attempt {attempt + 1}/{max_retries}), retrying in {wait_time}s: {last_error}")
                time.sleep(wait_time)
    
    # 모든 시도 실패
    raise RuntimeError(
        f"Backend result callback failed after {max_retries} attempts: {last_error}"
    )

def main() -> None:
    """
    메인 함수: 모든 예외를 catch하여 최소한 FAILED/TIMEOUT 결과는 전송하도록 보장합니다.
    """
    if not SUBMISSION_ID:
        # SUBMISSION_ID가 없으면 Job 자체가 잘못 생성된 것이므로 예외 발생
        raise RuntimeError("SUBMISSION_ID 환경 변수가 설정되어 있지 않습니다.")
    
    # 타임아웃 핸들러 설정 (2분 = 120초, 여유를 두고 110초로 설정)
    timeout_seconds = 110
    timeout_occurred = {"value": False}
    
    def timeout_handler(signum, frame):
        """타임아웃 발생 시 호출되는 핸들러"""
        timeout_occurred["value"] = True
        print(f"[Runner] Timeout after {timeout_seconds} seconds")
        # 타임아웃 결과를 전송하려고 시도
        try:
            timeout_result: Dict[str, Any] = {
                "status": "TIMEOUT",
                "score": 0,
                "fail_tags": ["timeout"],
                "feedback": [
                    {
                        "case": "timeout",
                        "message": f"채점 시간이 {timeout_seconds}초를 초과했습니다.",
                    }
                ],
            }
            send_result_to_backend(SUBMISSION_ID, timeout_result, timeout_seconds * 1000)
            print(f"[Runner] Timeout result sent to backend")
        except Exception as e:
            # Backend 콜백 실패해도 로그만 남기고 계속 진행
            print(f"[Runner] Failed to send timeout result: {e}")
        # TimeoutError를 발생시켜 최상위 핸들러에서 처리하도록 함
        # (최상위 핸들러에서 정상 종료)
        raise TimeoutError(f"Job timeout after {timeout_seconds} seconds")
    
    # Unix 시스템에서만 signal 사용 가능 (Windows에서는 작동하지 않을 수 있음)
    try:
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(timeout_seconds)
    except (AttributeError, OSError):
        # Windows나 signal을 지원하지 않는 환경에서는 무시
        print(f"[Runner] Warning: signal.SIGALRM not available, timeout handling disabled")
    
    try:
        print(f"[Runner] Start - submission_id={SUBMISSION_ID}")
        start_time = time.perf_counter()

        # 1) Redis에서 제출 데이터 로드 (예외 처리)
        try:
            submission = load_submission_from_redis(SUBMISSION_ID)
            code = submission.get("code", "")
            language = submission.get("language", "python")
        except Exception as e:
            # Redis 로드 실패 시 FAILED 결과 전송
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            error_result: Dict[str, Any] = {
                "status": "FAILED",
                "score": 0,
                "fail_tags": ["redis_error"],
                "feedback": [
                    {
                        "case": "redis_error",
                        "message": f"Redis에서 제출 데이터를 불러오는데 실패했습니다: {e}",
                    }
                ],
            }
            send_result_to_backend(SUBMISSION_ID, error_result, elapsed_ms)
            print(f"[Runner] Redis load error, fallback FAILED sent: {e}")
            return

        if not code:
            # 코드가 없으면 FAILED 결과 전송
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            error_result: Dict[str, Any] = {
                "status": "FAILED",
                "score": 0,
                "fail_tags": ["data_error"],
                "feedback": [
                    {
                        "case": "data_error",
                        "message": "Redis에서 코드를 찾을 수 없습니다.",
                    }
                ],
            }
            send_result_to_backend(SUBMISSION_ID, error_result, elapsed_ms)
            print(f"[Runner] Code not found, fallback FAILED sent")
            return
        
        # 2) 프롬프트 생성
        prompt = build_prompt(code=code, language=language)

        # 3) LLM 호출 + 시간 측정
        llm_start_time = time.perf_counter()
        try:
            llm_result = call_llm(prompt)
        except Exception as e:
            # LLM 에러 시에도 FAILED 결과를 백엔드에 전달
            elapsed_ms = int((time.perf_counter() - llm_start_time) * 1000)
            fallback_result: Dict[str, Any] = {
                "status": "FAILED", 
                "score": 0, 
                "fail_tags": ["llm_error"], 
                "feedback": [
                    {
                        "case": "llm_error", 
                        "message": f"채점 중 LLM 호출에 실패했습니다: {e}", 
                    }
                ],
            }
            send_result_to_backend(SUBMISSION_ID, fallback_result, elapsed_ms)
            print(f"[Runner] LLM error, fallback FAILED sent: {e}")
            return
        
        elapsed_ms = int((time.perf_counter() - llm_start_time) * 1000)

        # 4) 백엔드로 결과 콜백 (재시도 로직 포함)
        try:
            send_result_to_backend(SUBMISSION_ID, llm_result, elapsed_ms)
            print(
                f"[Runner] Done - submission_id={SUBMISSION_ID}, "
                f"status={llm_result['status']}, score={llm_result['score']}"
            )
        except Exception as e:
            # Backend 콜백 실패 시에도 최소한 로그는 남김
            # (이미 재시도를 했으므로 여기서는 로그만)
            print(f"[Runner] Critical: Failed to send result to backend after retries: {e}")
            # Backend 콜백 실패해도 최상위 핸들러에서 처리하도록 예외를 전파
            # (최상위 핸들러에서 FAILED 결과를 전송하고 정상 종료)
            raise
    
    except Exception as e:
        # 모든 예외를 catch하여 최소한 FAILED/TIMEOUT 결과는 전송
        # 타임아웃인지 확인
        if timeout_occurred["value"]:
            # 타임아웃 핸들러에서 이미 결과를 전송했을 수 있음
            # 하지만 전송 실패했을 수도 있으므로 다시 시도
            elapsed_ms = timeout_seconds * 1000
            error_result: Dict[str, Any] = {
                "status": "TIMEOUT",
                "score": 0,
                "fail_tags": ["timeout"],
                "feedback": [
                    {
                        "case": "timeout",
                        "message": f"채점 시간이 {timeout_seconds}초를 초과했습니다.",
                    }
                ],
            }
        else:
            # 일반 에러 (Backend 콜백 실패 등)
            elapsed_ms = int((time.perf_counter() - start_time) * 1000) if 'start_time' in locals() else 0
            error_result: Dict[str, Any] = {
                "status": "FAILED",
                "score": 0,
                "fail_tags": ["system_error"],
                "feedback": [
                    {
                        "case": "system_error",
                        "message": f"예상치 못한 오류가 발생했습니다: {e}",
                    }
                ],
            }
        
        # 결과 전송 시도 (실패해도 정상 종료)
        try:
            send_result_to_backend(SUBMISSION_ID, error_result, elapsed_ms)
            print(f"[Runner] Error result sent to backend: {error_result['status']}")
        except Exception as send_error:
            # Backend 콜백도 실패하면 로그만 남기고 정상 종료
            # (Job이 실패하지 않도록 함)
            print(f"[Runner] Critical: Cannot send error result to backend: {send_error}")
            print(f"[Runner] Error was: {e}")
        # 예외를 다시 발생시키지 않고 정상 종료 (exit code 0)
    finally:
        # 타임아웃 알림 해제
        try:
            signal.alarm(0)
        except (AttributeError, OSError):
            pass

if __name__ == "__main__":
    main()