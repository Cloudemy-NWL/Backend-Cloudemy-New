# app/db.py
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from motor.motor_asyncio import (
    AsyncIOMotorClient,
    AsyncIOMotorDatabase,
)

# .env 에서 읽는 설정
from app.config import settings


_client: Optional[AsyncIOMotorClient] = None
db: Optional[AsyncIOMotorDatabase] = None  # 다른 모듈에서 import 해서 사용


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI 애플리케이션 수명주기에 맞춰 MongoDB 연결/해제.
    main.py 에서: app = FastAPI(lifespan=lifespan)
    """
    global _client, db


    _client = AsyncIOMotorClient(
        settings.mongo_uri,      # MONGO_URI → mongo_uri
        uuidRepresentation="standard",
        serverSelectionTimeoutMS=5000,
    )

    await _client.admin.command("ping")


    db = _client[settings.db_name]    # DB_NAME → db_name

    # 인덱스 생성
    await db.submissions.create_index("status")
    await db.submissions.create_index("user_id")
    await db.submissions.create_index([("created_at", -1)])

    try:
        yield
    finally:
        if _client is not None:
            _client.close()


def get_db() -> AsyncIOMotorDatabase:
    """의존성 주입 또는 모듈 간 DB 접근"""
    if db is None:
        raise RuntimeError("MongoDB is not initialized. Did you attach lifespan?")
    return db


def submissions_coll():
    """submissions 컬렉션 헬퍼"""
    return get_db().submissions
