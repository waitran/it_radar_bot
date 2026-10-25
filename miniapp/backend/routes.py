from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from .auth import current_user
from .schemas import Event, EventPage, User

router = APIRouter(prefix='/api')


@router.get('/health')
def health(request: Request):
    request.app.state.catalog.health()
    return {'status': 'ok'}


@router.get('/me', response_model=User)
def me(user: User = Depends(current_user)):
    return user


@router.get('/events', response_model=EventPage, dependencies=[Depends(current_user)])
def events(request: Request, q: str = Query('', max_length=200),
           category: Literal['internships','events','contests','career'] | None = None,
           active_only: bool = False, sort: Literal['asc','desc'] = 'desc',
           page: int = Query(1, ge=1, le=10000), page_size: int = Query(20, ge=1, le=50)):
    return request.app.state.catalog.select(q=q.strip(), category=category,
        active_only=active_only, sort=sort, page=page, page_size=page_size)


@router.get('/events/{event_id:path}', response_model=Event, dependencies=[Depends(current_user)])
def event(request: Request, event_id: str):
    if len(event_id) > 512:
        raise HTTPException(422, 'Invalid event identifier')
    result = request.app.state.catalog.select(key=event_id, page_size=1)
    if not result['items']:
        raise HTTPException(404, 'Event not found')
    return result['items'][0]
