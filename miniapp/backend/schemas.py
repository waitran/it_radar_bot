from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, StrictInt


class User(BaseModel):
    id: StrictInt = Field(gt=0, le=2**52-1)
    first_name: str = Field(max_length=256)
    last_name: str | None = Field(default=None, max_length=256)
    username: str | None = Field(default=None, max_length=256)
    language_code: str | None = Field(default=None, max_length=64)


class Event(BaseModel):
    id: str = Field(max_length=512)
    title: str = Field(max_length=512)
    summary: str = Field(max_length=8000)
    url: str | None = Field(max_length=2048)
    apply_url: str | None = Field(max_length=2048)
    categories: list[str] = Field(max_length=4)
    published_at: datetime | None
    event_at: datetime | None
    deadline_at: datetime | None
    event_precision: Literal['date', 'time'] | None
    deadline_precision: Literal['date', 'time'] | None
    registration_status: Literal['open', 'closed', 'unknown']
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
