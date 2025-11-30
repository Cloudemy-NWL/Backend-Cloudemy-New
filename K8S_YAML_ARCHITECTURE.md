# Cloudemy Kubernetes YAML 아키텍처 가이드

이 문서는 Kubernetes YAML 파일 기준으로 Cloudemy 시스템의 각 컴포넌트 동작을 상세히 설명합니다.

---

## 📋 목차

1. [FE-BE 시나리오](#1-fe-be-시나리오)
2. [Frontend (YAML 기준)](#2-frontend-yaml-기준)
3. [Backend (YAML 기준)](#3-backend-yaml-기준)
4. [DB & Redis (YAML 기준)](#4-db--redis-yaml-기준)
5. [Scheduler 동작 (YAML 기준)](#5-scheduler-동작-yaml-기준)
6. [Runner Job 동작 (YAML 기준)](#6-runner-job-동작-yaml-기준)

---

## 1. FE-BE 시나리오

### 전체 플로우

```
사용자 브라우저
    ↓ HTTP 요청
http://cloudemy.local
    ↓
Ingress Controller
    ↓ 라우팅 (Ingress 규칙)
frontend-service (ClusterIP)
    ↓ 로드밸런싱
frontend Pod (포트 3000)
    ↓ API 요청
NEXT_PUBLIC_API_BASE_URL=http://backend:8000
    ↓
backend Service (ClusterIP)
    ↓ 로드밸런싱
backend Pod (포트 8000)
```

### YAML 파일 구성

| 컴포넌트 | YAML 파일 위치 | 리소스 타입 |
|---------|---------------|------------|
| Ingress | 프론트엔드 저장소 | `Ingress` |
| Frontend | 프론트엔드 저장소 | `Deployment` + `Service` |
| Backend | `k8s/backend.yaml` | `Deployment` + `Service` + `HPA` |

---

## 2. Frontend (YAML 기준)

### 2.1 Frontend Deployment (가정)

**위치**: 프론트엔드 저장소 (예: `frontend/k8s/frontend.yaml`)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: frontend-deploy
  labels:
    app: frontend
spec:
  replicas: 1
  selector:
    matchLabels:
      app: frontend
  template:
    metadata:
      labels:
        app: frontend
    spec:
      containers:
        - name: frontend
          image: <frontend-image>:latest
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 3000
          env:
            # Backend API 주소 (내부 Service 이름 사용)
            - name: NEXT_PUBLIC_API_BASE_URL
              value: "http://backend:8000"
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 512Mi
```

**주요 설정**:
- **포트**: `3000` (Next.js 기본 포트)
- **환경 변수**: `NEXT_PUBLIC_API_BASE_URL=http://backend:8000`
  - Kubernetes Service 이름 `backend`를 사용하여 내부 통신
  - `NEXT_PUBLIC_` 접두사로 클라이언트 사이드에서 접근 가능

### 2.2 Frontend Service (가정)

```yaml
apiVersion: v1
kind: Service
metadata:
  name: frontend-service
  labels:
    app: frontend
spec:
  type: ClusterIP  # 내부 통신용
  selector:
    app: frontend
  ports:
    - name: http
      port: 3000       # 서비스 포트
      targetPort: 3000 # 컨테이너 포트
```

**주요 설정**:
- **Type**: `ClusterIP` (클러스터 내부 통신)
- **포트**: `3000` → `3000`

### 2.3 Ingress (가정)

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: cloudemy-ingress
spec:
  rules:
    - host: cloudemy.local
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: frontend-service
                port:
                  number: 3000
```

**주요 설정**:
- **Host**: `cloudemy.local`
- **Path**: `/` → `frontend-service:3000`
- **외부 접근**: Ingress Controller를 통해 `http://cloudemy.local`로 접근

### 2.4 Frontend 동작 흐름

1. **사용자 요청**
   ```
   브라우저 → http://cloudemy.local
   ```

2. **Ingress 라우팅**
   ```
   Ingress Controller → frontend-service:3000
   ```

3. **Service 로드밸런싱**
   ```
   frontend-service → frontend Pod (포트 3000)
   ```

4. **API 요청**
   ```
   Frontend Pod → http://backend:8000 (내부 Service 이름)
   ```

---

## 3. Backend (YAML 기준)

### 3.1 Backend Deployment

**위치**: `k8s/backend.yaml`

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: backend-deploy
  labels:
    app: backend
spec:
  replicas: 1  # HPA가 자동으로 조정
  selector:
    matchLabels:
      app: backend
  template:
    metadata:
      labels:
        app: backend
    spec:
      containers:
        - name: backend
          image: withya61/cloudemy-backend:latest
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 8000
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 512Mi
          env:
            # MongoDB 연결
            - name: MONGO_URI
              value: "mongodb://mongo:27017"
            - name: DB_NAME
              value: "cloudemy"
            
            # Redis 큐 설정
            - name: REDIS_URL
              value: "redis://redis:6379"
            - name: QUEUE_SUBMISSIONS
              value: "queue:submissions"
            
            # Runner 관련 설정
            - name: K8S_NAMESPACE
              value: "default"
            - name: RUNNER_IMAGE
              value: withya61/cloudemy-runner:latest
            - name: BACKEND_INTERNAL_URL
              value: "http://backend:8000/internal"
          
          envFrom:
            - secretRef:
                name: cloudemy-secret  # INTERNAL_RESULT_TOKEN
```

**주요 설정**:
- **포트**: `8000` (FastAPI 기본 포트)
- **MongoDB**: `mongodb://mongo:27017` (Service 이름 사용)
- **Redis**: `redis://redis:6379` (Service 이름 사용)
- **Secret**: `cloudemy-secret`에서 `INTERNAL_RESULT_TOKEN` 주입

### 3.2 Backend Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: backend
  labels:
    app: backend
spec:
  type: ClusterIP  # 내부 통신용
  selector:
    app: backend
  ports:
    - name: http
      port: 8000       # 서비스 포트
      targetPort: 8000 # 컨테이너 포트
```

**주요 설정**:
- **Type**: `ClusterIP` (클러스터 내부 통신)
- **포트**: `8000` → `8000`
- **접근**: Frontend에서 `http://backend:8000`으로 접근

### 3.3 Backend HPA (Horizontal Pod Autoscaler)

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: backend-hpa
  labels:
    app: backend
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: backend-deploy
  minReplicas: 1
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: 80
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 60
      policies:
        - type: Percent
          value: 50
          periodSeconds: 60
    scaleUp:
      stabilizationWindowSeconds: 0
      policies:
        - type: Percent
          value: 100
          periodSeconds: 15
        - type: Pods
          value: 2
          periodSeconds: 15
      selectPolicy: Max
```

**주요 설정**:
- **스케일 타겟**: `backend-deploy` Deployment
- **최소 Pod**: `1개`
- **최대 Pod**: `10개`
- **CPU 임계값**: `70%`
- **Memory 임계값**: `80%`
- **스케일 업**: 즉시 반응 (최대 100% 증가 또는 2개 Pod 추가)
- **스케일 다운**: 60초 안정화 후 50% 감소

### 3.4 Backend 동작 흐름

1. **API 요청 수신**
   ```
   Frontend → backend Service (ClusterIP) → backend Pod
   ```

2. **데이터 저장**
   ```
   Backend Pod → MongoDB Service → MongoDB Pod
   ```

3. **채점 요청 큐 적재**
   ```
   Backend Pod → Redis Service → Redis Pod (queue:submissions)
   ```

4. **Runner 콜백 수신**
   ```
   Runner Job → backend Service → backend Pod (/internal/submissions/{id}/result)
   ```

---

## 4. DB & Redis (YAML 기준)

### 4.1 MongoDB Deployment

**위치**: `k8s/mongo.yaml`

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mongo-deploy
  labels:
    app: mongo
spec:
  replicas: 1
  selector:
    matchLabels:
      app: mongo
  template:
    metadata:
      labels:
        app: mongo
    spec:
      containers:
        - name: mongo
          image: mongo:6
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 27017
          command:
            - mongod
            - --bind_ip_all
          env:
            - name: MONGO_INITDB_DATABASE
              value: "cloudemy"
          volumeMounts:
            - name: mongo-data
              mountPath: /data/db
          livenessProbe:
            exec:
              command:
                - mongosh
                - --quiet
                - --eval
                - "db.runCommand({ ping: 1 }).ok"
            initialDelaySeconds: 30
            periodSeconds: 10
            timeoutSeconds: 5
          readinessProbe:
            exec:
              command:
                - mongosh
                - --quiet
                - --eval
                - "db.runCommand({ ping: 1 }).ok"
            initialDelaySeconds: 10
            periodSeconds: 5
            timeoutSeconds: 3
      volumes:
        - name: mongo-data
          emptyDir: {}
```

**주요 설정**:
- **이미지**: `mongo:6`
- **포트**: `27017`
- **데이터베이스**: `cloudemy`
- **볼륨**: `emptyDir` (임시 저장, 프로덕션에서는 PersistentVolume 사용 권장)
- **헬스체크**: Liveness/Readiness Probe로 MongoDB 상태 확인

### 4.2 MongoDB Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: mongo
  labels:
    app: mongo
spec:
  type: ClusterIP
  selector:
    app: mongo
  ports:
    - name: mongodb
      port: 27017      # 서비스 포트
      targetPort: 27017 # 컨테이너 포트
```

**주요 설정**:
- **Type**: `ClusterIP`
- **포트**: `27017` → `27017`
- **접근**: Backend에서 `mongodb://mongo:27017`로 접근

### 4.3 Redis Deployment

**위치**: `k8s/redis.yaml`

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: redis-deploy
  labels:
    app: redis
spec:
  replicas: 1
  selector:
    matchLabels:
      app: redis
  template:
    metadata:
      labels:
        app: redis
    spec:
      containers:
        - name: redis
          image: redis:7
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 6379
          command:
            - redis-server
            - --appendonly
            - "yes"
          volumeMounts:
            - name: redis-data
              mountPath: /data
          livenessProbe:
            exec:
              command:
                - redis-cli
                - ping
            initialDelaySeconds: 30
            periodSeconds: 10
            timeoutSeconds: 5
          readinessProbe:
            exec:
              command:
                - redis-cli
                - ping
            initialDelaySeconds: 5
            periodSeconds: 5
            timeoutSeconds: 3
      volumes:
        - name: redis-data
          emptyDir: {}
```

**주요 설정**:
- **이미지**: `redis:7`
- **포트**: `6379`
- **AOF**: `--appendonly yes` (데이터 영속성)
- **볼륨**: `emptyDir` (임시 저장, 프로덕션에서는 PersistentVolume 사용 권장)
- **헬스체크**: `redis-cli ping`으로 상태 확인

### 4.4 Redis Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: redis
  labels:
    app: redis
spec:
  type: ClusterIP
  selector:
    app: redis
  ports:
    - name: redis
      port: 6379       # 서비스 포트
      targetPort: 6379 # 컨테이너 포트
```

**주요 설정**:
- **Type**: `ClusterIP`
- **포트**: `6379` → `6379`
- **접근**: Backend/Scheduler에서 `redis://redis:6379`로 접근

### 4.5 DB & Redis 동작 흐름

1. **MongoDB 저장**
   ```
   Backend Pod → mongo Service → MongoDB Pod
   - 유저 정보
   - 과제 정보
   - 제출 결과
   ```

2. **Redis 큐 적재**
   ```
   Backend Pod → redis Service → Redis Pod
   - 큐 이름: queue:submissions
   - 메시지: { submission_id, language }
   ```

3. **Redis 데이터 저장**
   ```
   Backend Pod → redis Service → Redis Pod
   - 해시 키: submission:{submission_id}
   - 값: { submission_id, user_id, language, code }
   ```

---

## 5. Scheduler 동작 (YAML 기준)

### 5.1 Scheduler Deployment

**위치**: `k8s/scheduler.yaml`

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: scheduler-deploy
  labels:
    app: scheduler
spec:
  replicas: 1  # 중복 작업 방지를 위해 1개만 실행
  selector:
    matchLabels:
      app: scheduler
  template:
    metadata:
      labels:
        app: scheduler
    spec:
      containers:
        - name: scheduler
          image: withya61/cloudemy-scheduler:latest
          imagePullPolicy: IfNotPresent
          env:
            # Redis 큐 설정
            - name: REDIS_URL
              value: "redis://redis:6379"
            - name: QUEUE_SUBMISSIONS
              value: "queue:submissions"
            
            # Kubernetes / Runner 설정
            - name: K8S_NAMESPACE
              value: "default"
            - name: RUNNER_IMAGE
              value: withya61/cloudemy-runner:latest
            - name: BACKEND_INTERNAL_URL
              value: "http://backend:8000/internal"
          
          envFrom:
            - secretRef:
                name: cloudemy-secret  # LLM_API_KEY, INTERNAL_RESULT_TOKEN
```

**주요 설정**:
- **Replicas**: `1` (중복 작업 방지)
- **Redis 연결**: `redis://redis:6379`
- **큐 이름**: `queue:submissions`
- **Runner 이미지**: `withya61/cloudemy-runner:latest`
- **Secret**: `cloudemy-secret`에서 `LLM_API_KEY`, `INTERNAL_RESULT_TOKEN` 주입

### 5.2 Scheduler 동작 흐름

1. **Redis 큐 폴링**
   ```
   Scheduler Pod → redis Service → Redis Pod
   - blpop(queue:submissions, timeout=5초)
   - 새 작업 발견 시 메시지 수신
   ```

2. **Runner Job 생성**
   ```
   Scheduler Pod → Kubernetes API
   - Job 이름: runner-{submission_id}
   - 이미지: RUNNER_IMAGE (withya61/cloudemy-runner:latest)
   - 환경 변수 주입:
     * SUBMISSION_ID
     * REDIS_URL
     * BACKEND_INTERNAL_URL
     * INTERNAL_RESULT_TOKEN (Secret에서)
     * LLM_API_KEY (Secret에서)
   ```

3. **Job 생성 코드 (scheduler.py)**
   ```python
   # Kubernetes BatchV1Api 사용
   batch_api.create_namespaced_job(
       namespace=K8S_NAMESPACE,
       body=job
   )
   ```

### 5.3 Scheduler 환경 변수 매핑

| YAML 환경 변수 | Python 코드 변수 | 용도 |
|---------------|-----------------|------|
| `REDIS_URL` | `REDIS_URL` | Redis 연결 |
| `QUEUE_SUBMISSIONS` | `QUEUE_NAME` | 큐 이름 |
| `K8S_NAMESPACE` | `K8S_NAMESPACE` | Job 생성 네임스페이스 |
| `RUNNER_IMAGE` | `RUNNER_IMAGE` | Runner 컨테이너 이미지 |
| `BACKEND_INTERNAL_URL` | `BACKEND_INTERNAL_URL` | Runner 콜백 URL |
| `LLM_API_KEY` (Secret) | `LLM_API_KEY` | Runner에 전달 |
| `INTERNAL_RESULT_TOKEN` (Secret) | `RESULT_TOKEN` | Runner에 전달 |

---

## 6. Runner Job 동작 (YAML 기준)

### 6.1 Runner Job 생성 (동적)

**생성 위치**: Scheduler가 Kubernetes API를 통해 동적으로 생성

**Job 스펙** (scheduler.py에서 생성):

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: runner-{submission_id}  # 예: runner-507f1f77bcf86cd799439011
spec:
  backoffLimit: 1  # 실패 시 재시도 1번
  activeDeadlineSeconds: 120  # 2분 타임아웃
  template:
    metadata:
      labels:
        job: runner-{submission_id}
    spec:
      restartPolicy: Never  # Job은 재시작하지 않음
      containers:
        - name: runner
          image: withya61/cloudemy-runner:latest
          imagePullPolicy: IfNotPresent
          env:
            - name: SUBMISSION_ID
              value: "{submission_id}"  # Scheduler가 주입
            - name: REDIS_URL
              value: "redis://redis:6379"
            - name: BACKEND_INTERNAL_URL
              value: "http://backend:8000/internal"
            - name: INTERNAL_RESULT_TOKEN
              value: "{INTERNAL_RESULT_TOKEN}"  # Secret에서
            - name: LLM_API_KEY
              value: "{LLM_API_KEY}"  # Secret에서
            - name: LLM_MODEL
              value: "gpt-4o-mini"
```

**주요 설정**:
- **Job 이름**: `runner-{submission_id}` (DNS 규칙 준수)
- **재시도**: `backoffLimit: 1`
- **타임아웃**: `activeDeadlineSeconds: 120` (2분)
- **재시작 정책**: `Never` (Job은 완료되면 종료)

### 6.2 Runner Job 동작 흐름

1. **Job 생성**
   ```
   Scheduler Pod → Kubernetes API → Runner Job 생성
   ```

2. **Redis에서 제출 데이터 로드**
   ```
   Runner Pod → redis Service → Redis Pod
   - 키: submission:{submission_id}
   - 값: { submission_id, user_id, language, code }
   ```

3. **LLM 채점 실행**
   ```
   Runner Pod → OpenAI API
   - 프롬프트 생성
   - LLM 호출 (LLM_API_KEY 사용)
   - 채점 결과 파싱
   ```

4. **결과를 Backend로 전송**
   ```
   Runner Pod → backend Service → backend Pod
   - URL: http://backend:8000/internal/submissions/{submission_id}/result
   - Method: POST
   - Header: X-Result-Token: {INTERNAL_RESULT_TOKEN}
   - Body: { status, score, fail_tags, feedback, metrics }
   ```

5. **Job 완료**
   ```
   Runner Pod → 정상 종료 (exit code 0)
   - 성공/실패 여부와 관계없이 결과 전송 후 종료
   ```

### 6.3 Runner 환경 변수 매핑

| Job 환경 변수 | Python 코드 변수 | 용도 |
|--------------|-----------------|------|
| `SUBMISSION_ID` | `SUBMISSION_ID` | 제출 ID |
| `REDIS_URL` | `REDIS_URL` | Redis 연결 (제출 데이터 로드) |
| `BACKEND_INTERNAL_URL` | `BACKEND_INTERNAL_URL` | Backend 콜백 URL |
| `INTERNAL_RESULT_TOKEN` | `RESULT_TOKEN` | Backend 콜백 인증 |
| `LLM_API_KEY` | `LLM_API_KEY` | OpenAI API 키 |
| `LLM_MODEL` | `LLM_MODEL` | LLM 모델 이름 |

### 6.4 Runner Job 생명주기

```
생성 (Created)
    ↓
대기 (Pending)
    ↓
실행 (Running)
    ↓
완료 (Succeeded) 또는 실패 (Failed)
    ↓
Job 유지 (로그 확인용)
```

**Job 정리**:
- 완료된 Job은 수동으로 삭제하거나 TTL Controller 사용 권장
- 현재는 수동 삭제 필요: `kubectl delete job runner-{submission_id}`

---

## 📊 전체 YAML 파일 구조

```
k8s/
├── secret.yaml          # Secret (LLM_API_KEY, INTERNAL_RESULT_TOKEN)
├── mongo.yaml           # MongoDB Deployment + Service
├── redis.yaml           # Redis Deployment + Service
├── backend.yaml         # Backend Deployment + Service + HPA
└── scheduler.yaml       # Scheduler Deployment

frontend/k8s/ (가정)
├── frontend.yaml        # Frontend Deployment + Service
└── ingress.yaml         # Ingress (http://cloudemy.local)
```

---

## 🔄 전체 데이터 플로우

```
1. 사용자 요청
   브라우저 → Ingress → frontend-service → frontend Pod

2. API 요청
   frontend Pod → backend Service → backend Pod

3. 데이터 저장
   backend Pod → mongo Service → MongoDB Pod
   backend Pod → redis Service → Redis Pod (큐 적재)

4. 채점 처리
   Scheduler Pod → redis Service (큐 폴링)
   Scheduler Pod → Kubernetes API (Runner Job 생성)
   Runner Job → redis Service (제출 데이터 로드)
   Runner Job → OpenAI API (LLM 채점)
   Runner Job → backend Service (결과 전송)

5. 결과 저장
   backend Pod → mongo Service → MongoDB Pod (결과 업데이트)
```

---

## ✅ 요약

| 컴포넌트 | YAML 파일 | 주요 설정 |
|---------|----------|----------|
| **Frontend** | 프론트엔드 저장소 | Ingress, Service (ClusterIP), Deployment |
| **Backend** | `k8s/backend.yaml` | Service (ClusterIP), Deployment, HPA |
| **MongoDB** | `k8s/mongo.yaml` | Service (ClusterIP), Deployment |
| **Redis** | `k8s/redis.yaml` | Service (ClusterIP), Deployment |
| **Scheduler** | `k8s/scheduler.yaml` | Deployment (replicas: 1) |
| **Runner** | 동적 생성 | Job (Scheduler가 생성) |
| **Secret** | `k8s/secret.yaml` | LLM_API_KEY, INTERNAL_RESULT_TOKEN |

---

## 📝 참고사항

1. **Service 이름**: 모든 내부 통신은 Kubernetes Service 이름 사용
   - `backend`, `mongo`, `redis`, `frontend-service`

2. **Secret 관리**: 프로덕션에서는 Sealed Secrets 또는 External Secrets Operator 사용 권장

3. **볼륨**: 현재 `emptyDir` 사용 중, 프로덕션에서는 PersistentVolume 사용 권장

4. **Job 정리**: 완료된 Runner Job은 TTL Controller 또는 CronJob으로 정리 권장

5. **CORS 설정**: Backend의 CORS 설정에 `http://cloudemy.local` 추가 필요

