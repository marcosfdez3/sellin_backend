import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.google_auth import GoogleTokenError, verify_google_id_token
from app.core.rate_limit import LoginRateLimiter
from app.core.security import create_access_token, get_current_user, hash_password, verify_password
from app.models.models import PasswordResetCode, User
from app.schemas.schemas import (
    ForgotPasswordIn, GoogleLoginIn, ResetPasswordIn, TokenOut, UserCredentials, UserOut,
)
from app.services import mailer

auth_router = APIRouter(prefix="/auth", tags=["auth"])

rate_limiter = LoginRateLimiter(
    max_failed_per_email=settings.LOGIN_MAX_FAILED_PER_EMAIL,
    max_attempts_per_ip=settings.LOGIN_MAX_ATTEMPTS_PER_IP,
    window_seconds=settings.LOGIN_WINDOW_MINUTES * 60,
)

# Recuperar contraseña: como mucho 3 códigos por email y hora (para no llenar
# el buzón de nadie) y 10 peticiones por IP
forgot_limiter = LoginRateLimiter(
    max_failed_per_email=3,
    max_attempts_per_ip=10,
    window_seconds=3600,
    email_limit_message="Ya has pedido varios códigos para este email",
)
# Probar códigos: los mismos límites que el login
reset_limiter = LoginRateLimiter(
    max_failed_per_email=settings.LOGIN_MAX_FAILED_PER_EMAIL,
    max_attempts_per_ip=settings.LOGIN_MAX_ATTEMPTS_PER_IP,
    window_seconds=settings.LOGIN_WINDOW_MINUTES * 60,
)


def client_ip(request: Request) -> str:
    # Detrás de un proxy (Render), uvicorn debe arrancar con --proxy-headers
    # para que esto sea la IP real del cliente.
    return request.client.host if request.client else "unknown"


@auth_router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
async def register(credentials: UserCredentials, request: Request, db: AsyncSession = Depends(get_db)):
    """Crea un usuario y devuelve su token (queda con la sesión iniciada)."""
    rate_limiter.check(client_ip(request))

    email = credentials.email.lower()
    existing = await db.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ese email ya está registrado")

    user = User(email=email, hashed_password=hash_password(credentials.password))
    db.add(user)
    await db.flush()
    return TokenOut(access_token=create_access_token(user.id))


@auth_router.post("/login", response_model=TokenOut)
async def login(credentials: UserCredentials, request: Request, db: AsyncSession = Depends(get_db)):
    email = credentials.email.lower()
    rate_limiter.check(client_ip(request), email)

    user = await db.scalar(select(User).where(User.email == email))
    if user is None or user.hashed_password is None or not verify_password(credentials.password, user.hashed_password):
        rate_limiter.record_failure(email)
        if user is not None and user.hashed_password is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Esta cuenta usa Google. Entra con el botón de Google",
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email o contraseña incorrectos")

    rate_limiter.record_success(email)
    return TokenOut(access_token=create_access_token(user.id))


@auth_router.post("/google", response_model=TokenOut)
async def login_with_google(body: GoogleLoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Recibe el ID token que la app obtiene de Google, lo verifica y devuelve
    nuestro token. Crea la cuenta si no existe, o la vincula si ya había
    una con el mismo email.
    """
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Login con Google no configurado")
    rate_limiter.check(client_ip(request))

    try:
        claims = await verify_google_id_token(body.id_token)
    except GoogleTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No se pudo verificar la cuenta de Google")

    google_sub = claims["sub"]
    email = claims["email"].lower()

    user = await db.scalar(select(User).where(User.google_sub == google_sub))
    if user is None:
        # Google garantiza que el email está verificado: es seguro vincularlo
        user = await db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(email=email)
            db.add(user)
        user.google_sub = google_sub
        await db.flush()

    return TokenOut(access_token=create_access_token(user.id))


@auth_router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user


# ─── Recuperar contraseña ─────────────────────────────────────────────────────

def _hash_code(user_id: int, code: str) -> str:
    return hmac.new(settings.JWT_SECRET.encode(), f"{user_id}:{code}".encode(), hashlib.sha256).hexdigest()


@auth_router.post("/password/forgot", status_code=status.HTTP_204_NO_CONTENT)
async def forgot_password(
    body: ForgotPasswordIn,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Envía al email un código de 6 cifras para restablecer la contraseña.
    Responde igual exista o no la cuenta, para no revelar qué emails están
    registrados. Con una cuenta de Google sirve para añadirle contraseña.
    """
    if not mailer.is_configured() and not settings.DEBUG:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Recuperar la contraseña no está disponible ahora mismo",
        )
    email = body.email.lower()
    forgot_limiter.check(client_ip(request), email)
    forgot_limiter.record_failure(email)  # cuenta los códigos pedidos para ese email

    user = await db.scalar(select(User).where(User.email == email))
    if user is not None:
        code = f"{secrets.randbelow(10**6):06d}"
        await db.execute(delete(PasswordResetCode).where(PasswordResetCode.user_id == user.id))
        db.add(PasswordResetCode(
            user_id=user.id,
            code_hash=_hash_code(user.id, code),
            expires_at=datetime.utcnow() + timedelta(minutes=settings.PASSWORD_RESET_MINUTES),
        ))
        # El correo se manda tras responder: así la respuesta tarda lo mismo exista o no la cuenta
        background.add_task(mailer.send_password_reset_code, email, code)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@auth_router.post("/password/reset", response_model=TokenOut)
async def reset_password(body: ResetPasswordIn, request: Request, db: AsyncSession = Depends(get_db)):
    """Comprueba el código, cambia la contraseña y devuelve el token (sesión iniciada)."""
    email = body.email.lower()
    reset_limiter.check(client_ip(request), email)
    invalid = HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Código incorrecto o caducado")

    user = await db.scalar(select(User).where(User.email == email))
    entry = None
    if user is not None:
        entry = await db.scalar(select(PasswordResetCode).where(PasswordResetCode.user_id == user.id))
    if (
        entry is None
        or entry.expires_at < datetime.utcnow()
        or entry.attempts >= settings.PASSWORD_RESET_MAX_ATTEMPTS
    ):
        reset_limiter.record_failure(email)
        raise invalid

    if not hmac.compare_digest(entry.code_hash, _hash_code(user.id, body.code)):
        entry.attempts += 1
        # Guardar el intento ya: al lanzar el error, get_db desharía los cambios
        await db.commit()
        reset_limiter.record_failure(email)
        raise invalid

    user.hashed_password = hash_password(body.new_password)
    await db.delete(entry)
    reset_limiter.record_success(email)
    rate_limiter.record_success(email)  # desbloquea el login si estaba bloqueado
    return TokenOut(access_token=create_access_token(user.id))
