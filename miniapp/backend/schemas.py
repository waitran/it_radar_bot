from datetime import datetime
from pydantic import BaseModel, Field, StrictInt


class User(BaseModel):
    id: StrictInt = Field(gt=0, le=2**52-1)
    first_name: str = Field(max_length=256)
    last_name: str | None = Field(default=None, max_length=256)
    username: str | None = Field(default=None, max_length=256)
    language_code: str | None = Field(default=None, max_length=64)


class Event(BaseModel):
    id: str
    title: str
    summary: str
    url: str | None
    apply_url: str | None
    categories: list[str]
    published_at: datetime | None
    event_at: datetime | None
    deadline_at: datetime | None
    event_precision: str | None
    deadline_precision: str | None
    registration_status: str
    is_favorite: bool = False


class EventPage(BaseModel):
    items: list[Event]
    total: int
    page: int
    page_size: int


class Favorite(BaseModel):
    item_key: str
    created_at: datetime
    event: Event | None


class FavoritePage(BaseModel):
    items: list[Favorite]
    total: int
    page: int
    page_size: int


class FavoriteState(BaseModel):
    id: str
    is_favorite: bool
