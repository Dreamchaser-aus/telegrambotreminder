from datetime import datetime, timezone
from sqlalchemy import Integer, BigInteger, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from db import Base

class User(Base):
    __tablename__ = "users"

    id: Mapped[int]       = mapped_column(Integer, primary_key=True, index=True)
    chat_id: Mapped[int]  = mapped_column(BigInteger, unique=True, index=True)
    # 关键：存 UTC 带时区，显示时再转到 TZ
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )
