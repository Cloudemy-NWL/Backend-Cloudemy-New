import os
import json
import time
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

    resp = requests.post(
        url, 
        json=payload, 
        headers={"X-Result-Token": RESULT_TOKEN}, 
        timeout=10, 
    )

    if not resp.ok:
        raise RuntimeError(
            f"Backend result callback failed: {resp.status_code} / {resp.text}"
        )

def main() -> None:
    if not SUBMISSION_ID:
        raise RuntimeError("SUBMISSION_ID 환경 변수가 설정되어 있지 않습니다.")
        
    print(f"[Runner] Start - submission_id={SUBMISSION_ID}")

    # 1) Redis에서 제출 데이터 로드
    submission = load_submission_from_redis(SUBMISSION_ID)
    code = submission.get("code", "")
    language = submission.get("language", "python")

    if not code:
        raise RuntimeError("Redis에서 code를 찾을 수 없습니다.")
    
    # 2) 프롬프트 생성
    prompt = build_prompt(code=code, language=language)

    # 3) LLM 호출 + 시간 측정
    t0 = time.perf_counter()
    try:
        llm_result = call_llm(prompt)
    except Exception as e:
        # LLM 에러 시에도 FAILED 결과를 백엔드에 전달
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
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
    
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    # 4) 백엔드로 결과 콜백
    send_result_to_backend(SUBMISSION_ID, llm_result, elapsed_ms)
    print(
        f"[Runner] Done - submission_id={SUBMISSION_ID}, "
        f"status={llm_result['status']}, score={llm_result['score']}"
    )

if __name__ == "__main__":
    main()