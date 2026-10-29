"""Eval fixture: FastAPI APIRouter prefixes and verbs (parser input only)."""

import httpx
from fastapi import APIRouter

router = APIRouter(prefix="/v1/catalog")


def serialize(item_id):
    return {"id": item_id}


@router.get("/items/{item_id}")
def get_item(item_id):
    return serialize(item_id)


@router.put("/items/{item_id}")
def replace_item(item_id, data):
    return serialize(item_id)


@router.delete("/items/{item_id}")
def delete_item(item_id):
    httpx.post("https://audit.example.test/deletions/{item_id}")
    return {"deleted": item_id}
