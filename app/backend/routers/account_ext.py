from fastapi import APIRouter,Depends,Response,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..dependencies import get_current_session,get_db,get_settings
from ..config import Settings
from ..models import UserSession
from ..security import verify_password,hash_password
from ..timeutil import utcnow
from .auth import clear_auth_cookies

router=APIRouter(prefix='/api/auth',tags=['account'])
class PasswordRequest(BaseModel):
    current_password:str=Field(min_length=1,max_length=256)
    new_password:str=Field(min_length=10,max_length=256)

@router.post('/password')
def change_password(body:PasswordRequest,response:Response,identity=Depends(get_current_session),db:Session=Depends(get_db),settings:Settings=Depends(get_settings)):
    session,user=identity
    if not verify_password(body.current_password,user.password_hash):raise HTTPException(403,'当前密码不正确')
    user.password_hash=hash_password(body.new_password,settings.pbkdf2_iterations)
    for s in db.scalars(select(UserSession).where(UserSession.user_id==user.id,UserSession.revoked_at.is_(None))):s.revoked_at=utcnow()
    db.commit();clear_auth_cookies(response,settings)
    return {'ok':True}
