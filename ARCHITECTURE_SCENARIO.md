# Cloudemy 시스템 아키텍처 시나리오

## 📋 전체 플로우 개요

```
사용자 브라우저
    ↓
Ingress (http://cloudemy.local)
    ↓
frontend-service (ClusterIP)
    ↓
frontend Pod (포트 3000)
    ↓
backend Service (ClusterIP, http://backend:8000)
    ↓
backend Pod (포트 8000)
    ├─→ MongoDB (mongodb://mongo:27017)
    └─→ Redis 큐 (redis://redis:6379)
            ↓
        Scheduler Pod
            ↓
        Runner Job (동적 생성)
            ↓
        backend Service (http://backend:8000/internal)
            ↓
        backend Pod → MongoDB 업데이트
```

---

## 1️⃣ 사용자 → Frontend (Ingress 경유)

### 플로우
```
사용자 브라우저
    ↓ HTTP 요청
http://cloudemy.local
    ↓
Ingress Controller
    ↓ 라우팅 규칙
frontend-service (ClusterIP)
    ↓ 로드밸런싱
frontend Pod (포트 3000)
```

### 구현 상태
- ✅ **Ingress**: 프론트엔드 저장소에 설정 (가정)
  - Host: `cloudemy.local`
  - Path: `/` → `frontend-service:3000`
  
- ✅ **frontend-service**: 프론트엔드 저장소에 설정 (가정)
  - Type: `ClusterIP`
  - Port: `3000`
  - Selector: `app: frontend`

- ✅ **frontend Pod**: 프론트엔드 저장소에 설정 (가정)
  - Image: 프론트엔드 이미지
  - Port: `3000`
  - Environment: `NEXT_PUBLIC_API_BASE_URL=http://backend:8000`

---

## 2️⃣ Frontend → Backend (Service 기반 내부 통신)

### 플로우
```
frontend Pod
    ↓ API 요청
NEXT_PUBLIC_API_BASE_URL=http://backend:8000
    ↓
backend Service (ClusterIP)
    ↓ 로드밸런싱
backend Pod (포트 8000)
```

### 구현 상태
- ✅ **프론트엔드 환경 변수**: 프론트엔드 저장소에 설정 (가정)
  - `NEXT_PUBLIC_API_BASE_URL=http://backend:8000`

- ✅ **backend Service**: `k8s/backend.yaml`
  ```yaml
  type: ClusterIP
  name: backend
  port: 8000
  targetPort: 8000
  selector:
    app: backend
  ```

- ✅ **backend Pod**: `k8s/backend.yaml`
  - Image: `withya61/cloudemy-backend:latest`
  - Port: `8000`
  - CORS: 현재 `http://localhost:3000`만 허용
    - ⚠️ **주의**: Kubernetes 환경에서는 `http://cloudemy.local`도 허용 필요

---

## 3️⃣ Backend → MongoDB / Redis

### 플로우
```
backend Pod
    ├─→ MongoDB Service (mongodb://mongo:27017)
    │       ↓
    │   MongoDB Pod
    │       ↓
    │   데이터 저장 (유저/과제/결과)
    │
    └─→ Redis Service (redis://redis:6379)
            ↓
        Redis Pod
            ↓
        큐에 채점 요청 적재 (queue:submissions)
```

### 구현 상태
- ✅ **MongoDB 연결**: `k8s/backend.yaml`
  ```yaml
  env:
    - name: MONGO_URI
      value: "mongodb://mongo:27017"
    - name: DB_NAME
      value: "cloudemy"
  ```

- ✅ **MongoDB Service**: `k8s/mongo.yaml`
  - Type: `ClusterIP`
  - Name: `mongo`
  - Port: `27017`

- ✅ **Redis 연결**: `k8s/backend.yaml`
  ```yaml
  env:
    - name: REDIS_URL
      value: "redis://redis:6379"
    - name: QUEUE_SUBMISSIONS
      value: "queue:submissions"
  ```

- ✅ **Redis Service**: `k8s/redis.yaml`
  - Type: `ClusterIP`
  - Name: `redis`
  - Port: `6379`

- ✅ **백엔드 코드**: `backend/app/routers/submissions.py`
  - `create_submission()`: MongoDB에 저장 + Redis 큐에 메시지 적재

---

## 4️⃣ Scheduler → Runner Job 생성

### 플로우
```
Scheduler Pod
    ↓ 폴링 (blpop, timeout=5초)
Redis 큐 (queue:submissions)
    ↓ 새 작업 발견
Kubernetes API
    ↓ Job 생성
Runner Job (동적 생성)
    - Image: RUNNER_IMAGE (withya61/cloudemy-runner:latest)
    - Env: SUBMISSION_ID, REDIS_URL, BACKEND_INTERNAL_URL 등
```

### 구현 상태
- ✅ **Scheduler Pod**: `k8s/scheduler.yaml`
  - Image: `withya61/cloudemy-scheduler:latest`
  - Replicas: `1` (중복 작업 방지)
  - Environment:
    ```yaml
    REDIS_URL: "redis://redis:6379"
    QUEUE_SUBMISSIONS: "queue:submissions"
    K8S_NAMESPACE: "default"
    RUNNER_IMAGE: "withya61/cloudemy-runner:latest"
    BACKEND_INTERNAL_URL: "http://backend:8000/internal"
    ```

- ✅ **Scheduler 코드**: `scheduler/scheduler.py`
  - `pop_queue()`: Redis `blpop()`으로 큐 폴링
  - `create_runner_job()`: Kubernetes BatchV1Api로 Job 생성
  - Job 이름: `runner-{submission_id}`

- ✅ **Runner Job 설정**:
  - Image: `RUNNER_IMAGE` 환경 변수 사용
  - Restart Policy: `Never`
  - Backoff Limit: `1`
  - Active Deadline: `120초` (2분)

---

## 5️⃣ Runner → Backend 콜백

### 플로우
```
Runner Job Pod
    ↓ Redis에서 제출 데이터 로드
Redis (submission:{submission_id})
    ↓ LLM 채점 실행
LLM API (OpenAI)
    ↓ 채점 결과
POST http://backend:8000/internal/submissions/{id}/result
    ↓
backend Service (ClusterIP)
    ↓
backend Pod
    ↓
MongoDB 업데이트 (status, score, feedback 등)
```

### 구현 상태
- ✅ **Runner 코드**: `runner/runner.py`
  - `load_submission_from_redis()`: Redis에서 코드 로드
  - `call_llm()`: LLM으로 채점 수행
  - `send_result_to_backend()`: 결과를 Backend로 전송
    - URL: `BACKEND_INTERNAL_URL/submissions/{submission_id}/result`
    - Header: `X-Result-Token: {INTERNAL_RESULT_TOKEN}`
    - 재시도 로직 포함 (최대 2번)

- ✅ **Backend 내부 API**: `backend/app/routers/internal.py`
  - Endpoint: `POST /internal/submissions/{submission_id}/result`
  - 토큰 검증: `X-Result-Token` 헤더 확인
  - MongoDB 업데이트: `status`, `score`, `fail_tags`, `feedback`, `metrics`

- ✅ **Runner 환경 변수**: Scheduler가 Job 생성 시 주입
  ```yaml
  SUBMISSION_ID: {submission_id}
  REDIS_URL: "redis://redis:6379"
  BACKEND_INTERNAL_URL: "http://backend:8000/internal"
  INTERNAL_RESULT_TOKEN: {from secret}
  LLM_API_KEY: {from secret}
  LLM_MODEL: "gpt-4o-mini"
  ```

---

## 6️⃣ Backend HPA 자동 확장

### 플로우
```
트래픽 증가
    ↓
Backend Pod CPU/메모리 사용률 증가
    ↓
HPA 모니터링
    ↓
CPU 70% 또는 Memory 80% 초과
    ↓
Backend Deployment 스케일 아웃
    ↓
Pod 수 증가 (1 → 최대 10개)
    ↓
로드 분산
```

### 구현 상태
- ✅ **HPA 설정**: `k8s/backend.yaml`
  ```yaml
  apiVersion: autoscaling/v2
  kind: HorizontalPodAutoscaler
  metadata:
    name: backend-hpa
  spec:
    scaleTargetRef:
      name: backend-deploy
    minReplicas: 1
    maxReplicas: 10
    metrics:
      - type: Resource
        resource:
          name: cpu
          target:
            averageUtilization: 70
      - type: Resource
        resource:
          name: memory
          target:
            averageUtilization: 80
  ```

- ✅ **스케일 업 정책**:
  - Stabilization Window: `0초` (즉시 반응)
  - Policies:
    - Percent: `100%` 증가 (15초마다)
    - Pods: `2개` 증가 (15초마다)
    - Select Policy: `Max` (둘 중 큰 값 선택)

- ✅ **스케일 다운 정책**:
  - Stabilization Window: `60초`
  - Policy: `50%` 감소 (60초마다)

- ✅ **Backend Pod 리소스**: `k8s/backend.yaml`
  ```yaml
  resources:
    requests:
      cpu: 100m
      memory: 128Mi
    limits:
      cpu: 500m
      memory: 512Mi
  ```

---

## 📊 전체 아키텍처 다이어그램

```
┌─────────────────────────────────────────────────────────────────┐
│                    Kubernetes Cluster                            │
│                                                                  │
│  ┌──────────────┐                                               │
│  │   Ingress    │  http://cloudemy.local                        │
│  │  Controller  │                                               │
│  └──────┬───────┘                                               │
│         │                                                        │
│         ↓                                                        │
│  ┌──────────────┐      ┌──────────────┐                        │
│  │  frontend    │──────│  backend     │                        │
│  │  Service     │      │  Service     │                        │
│  │ (ClusterIP)  │      │ (ClusterIP)  │                        │
│  └──────┬───────┘      └──────┬───────┘                        │
│         │                     │                                 │
│         ↓                     ↓                                 │
│  ┌──────────────┐      ┌──────────────┐                        │
│  │  frontend    │      │  backend     │◄──┐                    │
│  │  Pod :3000   │      │  Pod :8000   │   │                    │
│  └──────────────┘      └──────┬───────┘   │                    │
│                               │           │                    │
│                    ┌──────────┴──────┐    │                    │
│                    │                 │    │                    │
│                    ↓                 ↓    │                    │
│            ┌──────────────┐  ┌──────────────┐                 │
│            │   MongoDB    │  │    Redis     │                 │
│            │  Service     │  │   Service    │                 │
│            │  :27017      │  │    :6379     │                 │
│            └──────┬───────┘  └──────┬───────┘                 │
│                   │                 │                          │
│                   ↓                 ↓                          │
│            ┌──────────────┐  ┌──────────────┐                 │
│            │   MongoDB    │  │    Redis     │                 │
│            │    Pod       │  │     Pod      │                 │
│            └──────────────┘  └──────┬───────┘                 │
│                                     │                          │
│                                     ↓                          │
│                            ┌──────────────┐                    │
│                            │  Scheduler   │                    │
│                            │     Pod      │                    │
│                            └──────┬───────┘                    │
│                                   │                            │
│                                   ↓                            │
│                            ┌──────────────┐                    │
│                            │  Runner Job  │                    │
│                            │  (동적 생성)  │                    │
│                            └──────────────┘                    │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  HPA (backend-hpa)                                      │  │
│  │  - CPU 70% / Memory 80% 기준                            │  │
│  │  - 1~10개 Pod 자동 스케일링                             │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## ✅ 시나리오 검증 결과

### 완전히 구현됨 ✅
1. ✅ Backend Service (ClusterIP) - `k8s/backend.yaml`
2. ✅ Backend → MongoDB 저장 - `backend/app/routers/submissions.py`
3. ✅ Backend → Redis 큐 적재 - `backend/app/routers/submissions.py`
4. ✅ Scheduler → Redis 큐 폴링 - `scheduler/scheduler.py`
5. ✅ Scheduler → Runner Job 생성 - `scheduler/scheduler.py`
6. ✅ Runner → Backend 콜백 - `runner/runner.py`
7. ✅ Backend → 결과 DB 저장 - `backend/app/routers/internal.py`
8. ✅ HPA 자동 스케일링 - `k8s/backend.yaml`

### 프론트엔드 저장소에 있다고 가정 ✅
1. ✅ Frontend Pod/Deployment
2. ✅ frontend-service (ClusterIP)
3. ✅ Ingress (http://cloudemy.local)
4. ✅ 프론트엔드 환경 변수 (`NEXT_PUBLIC_API_BASE_URL=http://backend:8000`)

### 수정 권장 사항 ⚠️
1. ⚠️ **CORS 설정**: `backend/app/main.py`
   - 현재: `allow_origins=["http://localhost:3000"]`
   - 권장: `allow_origins=["http://localhost:3000", "http://cloudemy.local"]`
   - 또는 환경 변수로 관리

---

## 🎯 결론

**시나리오가 정확합니다!** 

프론트엔드 YAML 파일이 프론트엔드 저장소에 있다는 가정 하에, 모든 플로우가 올바르게 설계되어 있고 백엔드 저장소에는 이미 구현되어 있습니다.

단, CORS 설정만 Kubernetes 환경에 맞게 수정하면 완벽합니다.

