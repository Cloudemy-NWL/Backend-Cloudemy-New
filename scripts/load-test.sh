#!/bin/bash

# 원클릭 폭주쇼 - HPA 시연용 부하 테스트 스크립트
# 사용법: ./load-test.sh [BACKEND_URL] [REQUEST_COUNT] [CONCURRENT]

BACKEND_URL="${1:-http://localhost:8000}"
REQUESTS="${2:-100}"
CONCURRENT="${3:-20}"

echo "🚀 원클릭 폭주쇼 시작!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📍 Backend URL: $BACKEND_URL"
echo "📊 총 요청 수: $REQUESTS"
echo "⚡ 동시 요청 수: $CONCURRENT"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 테스트용 코드 (간단한 Python 코드)
TEST_CODE='print("Hello, World!")'

# 부하 테스트 실행
echo "🔥 부하 발생 중..."
ab -n "$REQUESTS" -c "$CONCURRENT" -p /dev/stdin -T "application/json" \
  "$BACKEND_URL/submissions" <<EOF
{"language": "python", "code": "$TEST_CODE"}
EOF

echo ""
echo "✅ 부하 테스트 완료!"
echo "💡 다음 명령어로 HPA 상태 확인:"
echo "   kubectl get hpa backend-hpa -w"
echo "   kubectl get pods -l app=backend -w"

