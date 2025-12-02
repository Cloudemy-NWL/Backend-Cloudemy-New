import os
import json
import time
from typing import Any, Dict, Tuple
from redis import Redis
from kubernetes import client, config

# 환경 변수
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
QUEUE_NAME = os.getenv("QUEUE_SUBMISSIONS", "queue:submissions")

# job을 생성할 네임스페이스(k8s Deployment가 도는 곳)
K8S_NAMESPACE = os.getenv("K8S_NAMESPACE", "default")

# runner Job에서 사용할 컨테이너 이미지
RUNNER_IMAGE = os.getenv("RUNNER_IMAGE", "cloudemy/runner:latest")

# runner 에 넘길 공통 env 값들 (runner.py와 동일하게 맞춰줌)
BACKEND_INTERNAL_URL = os.getenv("BACKEND_INTERNAL_URL", "http://backend:8000/api/internal")
RESULT_TOKEN = os.getenv("INTERNAL_RESULT_TOKEN", "secret")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

# ⭐ 로그 추가: 모듈 로드 시 현재 설정 출력
print(
    f"[Scheduler] 모듈 로드 완료. "
    f"REDIS_URL={REDIS_URL}, QUEUE_NAME={QUEUE_NAME}, "
    f"K8S_NAMESPACE={K8S_NAMESPACE}, RUNNER_IMAGE={RUNNER_IMAGE}, "
    f"BACKEND_INTERNAL_URL={BACKEND_INTERNAL_URL}, LLM_MODEL={LLM_MODEL}"
)


# Kubernetes 설정 로드
def init_k8s_client() -> client.BatchV1Api:
    # ⭐ 로그 추가
    print("[Scheduler] Kubernetes 클라이언트 초기화 시작")
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        # Pod 안에서 실행되는 경우
        print("[Scheduler] 인클러스터 환경으로 감지됨. InClusterConfig 사용")
        config.load_incluster_config()
    else:
        # 로컬에서 테스트용
        print("[Scheduler] 로컬 환경으로 감지됨. kubeconfig 사용")
        config.load_kube_config()

    print("[Scheduler] Kubernetes 클라이언트 초기화 완료")
    return client.BatchV1Api()


# Redis에서 작업 하나 꺼내기
def pop_queue(r: Redis) -> Dict[str, Any] | None:
    # ⭐ 로그 추가
    print(f"[Scheduler] Redis 큐에서 작업 대기 중... queue='{QUEUE_NAME}' (최대 5초 블록)")
    item: Tuple[str, str] | None = r.blpop(QUEUE_NAME, timeout=5)
    if not item:
        # ⭐ 로그 추가
        print("[Scheduler] 새 작업 없음(타임아웃). 다시 대기")
        return None

    key, raw = item
    # ⭐ 로그 추가
    print(f"[Scheduler] Redis에서 작업 수신. key={key}, raw='{raw}'")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[Scheduler] 큐 데이터 JSON 파싱 실패: raw={raw} / error={e}")
        return None

    if "submission_id" not in data:
        print(f"[Scheduler] submission_id 필드가 없습니다. data={data}")
        return None

    print(f"[Scheduler] 유효한 작업 수신. submission_id={data['submission_id']}")
    return data


# Runner Job 생성
def create_runner_job(
    batch_api: client.BatchV1Api,
    submission_id: str,
) -> None:
    # Job 이름은 DNS 규칙 때문에 소문자/숫자/하이픈 정도만 허용되니 살짝 정리
    safe_id = submission_id.lower().replace("_", "-")
    job_name = f"runner-{safe_id}"[:63]  # k8s 이름 길이 제한

    print(f"[Scheduler] Runner Job 생성 시작. job_name={job_name}, submission_id={submission_id}")

    # runner 컨테이너 정의
    container = client.V1Container(
        name="runner",
        image=RUNNER_IMAGE,
        image_pull_policy="IfNotPresent",
        env=[
            client.V1EnvVar(name="SUBMISSION_ID", value=submission_id),
            client.V1EnvVar(name="REDIS_URL", value=REDIS_URL),
            client.V1EnvVar(name="BACKEND_INTERNAL_URL", value=BACKEND_INTERNAL_URL),
            client.V1EnvVar(name="INTERNAL_RESULT_TOKEN", value=RESULT_TOKEN),
            client.V1EnvVar(name="LLM_API_KEY", value=LLM_API_KEY),
            client.V1EnvVar(name="LLM_MODEL", value=LLM_MODEL),
        ],
    )

    pod_spec = client.V1PodSpec(
        restart_policy="Never",   # job 이 재시작 시킴 
        containers=[container],
    )

    pod_template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels={"job": job_name}),
        spec=pod_spec,
    )

    job_spec = client.V1JobSpec(
        template=pod_template,
        backoff_limit=1,  # 실패 시 재시도 횟수
        active_deadline_seconds=120,  # 2분
    )

    job = client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(name=job_name),
        spec=job_spec,
    )

    try:
        batch_api.create_namespaced_job(
            namespace=K8S_NAMESPACE,
            body=job,
        )
        # ⭐ 로그 추가
        print(f"[Scheduler] Runner Job 생성 완료. namespace={K8S_NAMESPACE}, job_name={job_name}")
    except Exception as e:
        # ⭐ 로그 추가
        print(f"[Scheduler] Runner Job 생성 실패. job_name={job_name}, error={e}")
        raise


# 메인 루프
def main() -> None:
    print("[Scheduler] 시작")

    # 1) k8s 클라이언트 준비
    batch_api = init_k8s_client()

    # 2) Redis 연결
    print(f"[Scheduler] Redis 연결 시도. url={REDIS_URL}")  # ⭐ 로그 추가
    r = Redis.from_url(REDIS_URL, decode_responses=True)
    print("[Scheduler] Redis 연결 성공")  # ⭐ 로그 추가

    try:
        while True:
            # 큐에서 작업 하나 가져오기 (없으면 5초 동안 대기)
            msg = pop_queue(r)
            if msg is None:
                # ⭐ 로그 추가
                # 너무 시끄럽지 않게 1초 정도 쉬고 다시 대기
                time.sleep(1)
                continue

            submission_id = msg["submission_id"]
            print(f"[Scheduler] 새 작업 처리 시작. submission_id={submission_id}")  # ⭐ 로그 추가

            try:
                create_runner_job(batch_api, submission_id)
                print(f"[Scheduler] Job 생성 성공. submission_id={submission_id}")  # ⭐ 로그 추가
            except Exception as e:
                print(f"[Scheduler] Job 생성 실패. submission_id={submission_id}, error={e}")

    finally:
        print("[Scheduler] Redis 연결 종료")  # ⭐ 로그 추가
        r.close()


if __name__ == "__main__":
    main()
