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
BACKEND_INTERNAL_URL = os.getenv("BACKEND_INTERNAL_URL", "http://backend:8000/api/internal")
RESULT_TOKEN = os.getenv("INTERNAL_RESULT_TOKEN", "secret")

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

client = OpenAI(api_key=LLM_API_KEY)

# 모듈 로드 시 한 번 찍히는 로그
print(
    f"[Runner] 모듈 로드 완료. "
    f"SUBMISSION_ID={SUBMISSION_ID}, REDIS_URL={REDIS_URL}, "
    f"BACKEND_INTERNAL_URL={BACKEND_INTERNAL_URL}, LLM_MODEL={LLM_MODEL}"
)

# Redis에서 제출 데이터 로드
def load_submission_from_redis(submission_id: str) -> Dict[str, Any]:

    print(f"[Runner] Redis에서 제출 데이터 로드 시작. submission_id={submission_id}")
    r = Redis.from_url(REDIS_URL, decode_responses=True)
    try: 
        key = f"submission:{submission_id}"
        print(f"[Runner] Redis HGETALL 호출. key='{key}'")
        data = r.hgetall(key)
        if not data:
            print(f"[Runner] Redis에 해당 키 데이터가 없습니다. key='{key}'")
            raise RuntimeError(f"Redis에서 데이터를 찾을 수 없습니다: {key}")

        # 코드 전체는 길 수 있으니 앞부분만 로그로 출력
        code_preview = (data.get("code") or "")[:80].replace("\n", "\\n")
        print(
            f"[Runner] Redis 로드 성공. "
            f"필드={list(data.keys())}, language={data.get('language')}, "
            f"code 미리보기='{code_preview}'"
        )

        return data

    finally:
        r.close()


# LLM에게 넘길 프롬프트 생성 함수
def build_prompt(code: str, language: str = "python") -> str:
    print(f"[Runner] 프롬프트 생성. language={language}, code 길이={len(code)}")
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
    text_preview = text[:200].replace("\n", "\\n")
    print(f"[Runner] LLM 원본 출력 미리보기: '{text_preview}'")


    # 3) json 파싱
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:

        print(f"[Runner] LLM 결과 JSON 파싱 실패: {e}")
        raise RuntimeError(
            f"JSON parsing failed: {e}\n--- LLM Output ---\n{text}"
        ) from e

    # 4) 필수 필드 검증
    for field in ("status", "score", "fail_tags", "feedback"):
        if field not in data:

            print(f"[Runner] LLM 결과에 필수 필드 누락: {field}")
            raise RuntimeError(
                f"Mission field in LLM result: {field}\n--- LLM Output ---\n{text}"
            )
    print(
        f"[Runner] LLM 파싱 결과 요약. "
        f"status={data.get('status')}, score={data.get('score')}, "
        f"fail_tags={data.get('fail_tags')}"
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
    print (
        f"[Runner] 백엔드로 결과 전송 시작. "
        f"url={url}, status={payload['status']}, score={payload['score']}, "
        f"timeMs={elapsed_ms}, fail_tags={payload['fail_tags']}"
    )

    last_error = None
    for attempt in range(max_retries):
        try:
            print(f"[Runner] 백엔드 POST 시도 {attempt + 1}/{max_retries}")
            resp = requests.post(
                url, 
                json=payload, 
                headers={"X-Result-Token": RESULT_TOKEN}, 
                timeout=10, 
            )
            
            if resp.ok:
                print(f"[Runner] 백엔드 콜백 성공. HTTP {resp.status_code}")
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
    print(f"[Runner] 백엔드 결과 콜백이 {max_retries}번 모두 실패했습니다: {last_error}")
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
        print(f"[Runner] 타임아웃 발생. {timeout_seconds}초 초과")

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

            print(f"[Runner] 타임아웃 결과를 백엔드로 전송 완료")
            
        except Exception as e:
            # Backend 콜백 실패해도 로그만 남기고 계속 진행
            print(f"[Runner] 타임아웃 결과 전송 실패: {e}")
            
        # TimeoutError를 발생시켜 최상위 핸들러에서 처리하도록 함
        raise TimeoutError(f"Job timeout after {timeout_seconds} seconds")
    
    # Unix 시스템에서만 signal 사용 가능 (Windows에서는 작동하지 않을 수 있음)
    try:
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(timeout_seconds)
    except (AttributeError, OSError):
        # Windows나 signal을 지원하지 않는 환경에서는 무시
        print("[Runner] 경고: signal.SIGALRM 을 사용할 수 없어 타임아웃 처리 비활성화됨")
    
    try:
        print(f"[Runner] 실행 시작. submission_id={SUBMISSION_ID}")
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
            print(f"[Runner] Redis 로드 에러, FAILED 결과 전송: {e}")
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
            print("[Runner] 코드가 비어 있음. FAILED 결과 전송")
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
            print(f"[Runner] LLM 호출 에러, FAILED 결과 전송: {e}")
            return
        
        elapsed_ms = int((time.perf_counter() - llm_start_time) * 1000)
        print(f"[Runner] LLM 호출 완료. 소요 시간={elapsed_ms}ms")

        # 4) 백엔드로 결과 콜백 (재시도 로직 포함)
        try:
            send_result_to_backend(SUBMISSION_ID, llm_result, elapsed_ms)
            print(
                f"[Runner] 작업 완료. submission_id={SUBMISSION_ID}, "
                f"status={llm_result['status']}, score={llm_result['score']}"
            )
        except Exception as e:

            # Backend 콜백 실패 시에도 최소한 로그는 남김
            print(f"[Runner] 치명적 오류: 재시도 후에도 백엔드 결과 전송에 실패했습니다: {e}")

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
        print(f"[Runner] 최상위 예외 처리. status={error_result['status']}, error={e}")

        
        # 결과 전송 시도 (실패해도 정상 종료)
        try:
            send_result_to_backend(SUBMISSION_ID, error_result, elapsed_ms)
            print(f"[Runner] 오류 결과를 백엔드로 전송 완료. status={error_result['status']}")

        except Exception as send_error:
            
            print(f"[Runner] 치명적 오류: 오류 결과 자체도 백엔드로 전송하지 못했습니다: {send_error}")
            print(f"[Runner] 원래 오류: {e}")

        # 예외를 다시 발생시키지 않고 정상 종료 (exit code 0)
    finally:
        # 타임아웃 알림 해제
        try:
            signal.alarm(0)
        except (AttributeError, OSError):
            pass

if __name__ == "__main__":
    main()