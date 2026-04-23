import uuid
from sqlalchemy import String, Text, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from migration.db_factory import Base

class SavedItem(Base):
    __tablename__ = "saved_items"
    __table_args__ = {"schema": "app"}   # add this

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    profile_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    raw_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_crawl")
    url_source: Mapped[str] = mapped_column(String, nullable=False, server_default="generic", index=True)

    __table_args__ = (
    UniqueConstraint("profile_id", "canonical_url", name="ux_saved_items_profile_canonical"),
    Index("idx_saved_items_profile_canonical", "profile_id", "canonical_url"),
    )


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    auth_provider: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)