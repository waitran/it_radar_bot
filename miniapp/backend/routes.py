from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from .auth import current_user
from .schemas import Event, EventPage, User, FavoritePage, FavoriteState

router = APIRouter(prefix='/api')


@router.get('/health')
def health(request: Request):
    request.app.state.catalog.health()
    request.app.state.favorites.health()
    return {'status': 'ok'}


@router.get('/me', response_model=User)
def me(user: User = Depends(current_user)):
    return user


@router.get('/events', response_model=EventPage)
def events(request: Request, q: str = Query('', max_length=200),
           category: Literal['internships','events','contests','career'] | None = None,
           active_only: bool = False, sort: Literal['asc','desc'] = 'desc',
           page: int = Query(1, ge=1, le=10000), page_size: int = Query(20, ge=1, le=50),
           user: User = Depends(current_user)):
    result = request.app.state.catalog.select(q=q.strip(), category=category,
        active_only=active_only, sort=sort, page=page, page_size=page_size)
    mark_favorites(request, user.id, result['items'])
    return result


@router.get('/events/{event_id:path}', response_model=Event)
def event(request: Request, event_id: str, user: User = Depends(current_user)):
    if len(event_id) > 512:
        raise HTTPException(422, 'Invalid event identifier')
    result = request.app.state.catalog.select(key=event_id, page_size=1)
    if not result['items']:
        raise HTTPException(404, 'Event not found')
    mark_favorites(request, user.id, result['items'])
    return result['items'][0]


def mark_favorites(request, uid, items):
    saved = request.app.state.favorites.keys(uid, [item['id'] for item in items])
    for item in items:
        item['is_favorite'] = item['id'] in saved


def validate_id(event_id):
    if not event_id or len(event_id) > 512:
        raise HTTPException(422, 'Invalid event identifier')


@router.get('/favorites', response_model=FavoritePage)
def favorites(request: Request, page: int = Query(1, ge=1, le=10000),
              page_size: int = Query(20, ge=1, le=50), user: User = Depends(current_user)):
    rows, total = request.app.state.favorites.page(user.id, page, page_size)
    events = request.app.state.catalog.select(keys=[row['item_key'] for row in rows], page_size=50)['items'] if rows else []
    by_key = {item['id']: dict(item, is_favorite=True) for item in events}
    return dict(items=[dict(row, event=by_key.get(row['item_key'])) for row in rows],
                total=total, page=page, page_size=page_size)


@router.put('/favorites/{event_id:path}', response_model=FavoriteState)
def save_favorite(request: Request, event_id: str, user: User = Depends(current_user)):
    validate_id(event_id)
    if not request.app.state.catalog.select(key=event_id, page_size=1)['items']:
        raise HTTPException(404, 'Event not found')
    request.app.state.favorites.save(user.id, event_id)
    return dict(id=event_id, is_favorite=True)


@router.delete('/favorites/{event_id:path}', response_model=FavoriteState)
def remove_favorite(request: Request, event_id: str, user: User = Depends(current_user)):
    validate_id(event_id)
    request.app.state.favorites.remove(user.id, event_id)
    return dict(id=event_id, is_favorite=False)
