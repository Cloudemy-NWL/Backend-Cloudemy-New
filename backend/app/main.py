# app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.db import lifespan
from app.routers import submissions, internal,debug

# lifespan=lifespan ➜ MongoDB 연결/해제를 자동으로 수행
app = FastAPI(lifespan=lifespan) 

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 라우터 등록
app.include_router(submissions.router)
app.include_router(internal.router)
app.include_router(debug.router)
