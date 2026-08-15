from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr

from app.models.user import UserRole


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: UUID
    # str y no EmailStr A PROPÓSITO: esto es lo que SALE, y el correo ya se
    # valida al entrar (UserCreate / UserUpdate). Validándolo otra vez aquí, una
    # sola fila con un correo que Pydantic no acepta —el INITIAL_ADMIN_EMAIL de
    # una instalación de pruebas con dominio `.local`, por ejemplo— tumbaba la
    # pantalla de Usuarios ENTERA con un 500, y desde el panel ya no había forma
    # ni de verlo ni de arreglarlo.
    email: str
    role: UserRole
    nombre: str | None = None
    activo: bool = True
    last_login: datetime | None = None

    class Config:
        from_attributes = True


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
