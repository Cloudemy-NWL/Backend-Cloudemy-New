from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # .env 파일 위치를 backend/app 기준에서 상위 폴더로
    model_config = SettingsConfigDict(
        env_file="../.env",
        env_file_encoding="utf-8",
        extra="ignore",     # 모르는 env값 들어와도 무시하고 넘어감
    )

    # .env KEY 이름은 자동으로 snake_case로 변환됨
    # 예: MONGO_URI → mongo_uri
    mongo_uri: str
    db_name: str

settings = Settings()
