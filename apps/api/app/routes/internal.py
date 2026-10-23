"""Routes only nginx calls on the platform host (`/internal/...`; nginx serves nothing under
/internal to the internet, see infra/platform/host/nginx/groundcrew.conf)."""

from typing import Annotated

from fastapi import APIRouter, Header, Response, status

from app import registry
from app.routes.deps import SettingsDep

router = APIRouter(prefix="/internal", include_in_schema=False)


@router.get("/registry/auth")
async def registry_auth(
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
    x_original_uri: Annotated[str, Header()] = "",
    x_original_method: Annotated[str, Header()] = "GET",
) -> Response:
    """nginx's auth_request for /v2/ (the Groundcrew registry): 200 lets the pull through, 401
    makes nginx ask for credentials."""
    ok = registry.check_access(settings, authorization, x_original_uri, x_original_method)
    return Response(status_code=status.HTTP_200_OK if ok else status.HTTP_401_UNAUTHORIZED)
