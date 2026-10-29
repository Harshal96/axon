
from pydantic import BaseModel


class PushEvent(BaseModel):
    sha: str
    branch: str = "main"
    repository: str = "axon"
    changed_files: list[str] = []
    author: str = ""
    workspace_root: str = "."
