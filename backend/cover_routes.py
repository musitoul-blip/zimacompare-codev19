from fastapi import APIRouter

router = APIRouter(prefix="/api/cover")


@router.get("/ping")
def ping():
    return {"ok": True}
