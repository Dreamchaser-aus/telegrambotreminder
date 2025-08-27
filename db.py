# db.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from config import DATABASE_URL

# 连接引擎（Postgres/SQLite 自动适配）
engine = create_engine(
    DATABASE_URL,
    future=True,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

def init_db():
    # 导入 models 再创建表，避免循环引用
    import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
