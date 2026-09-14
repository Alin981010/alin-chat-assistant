from pydantic import BaseModel, ConfigDict


class MemoryContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    user_id: str = "local-user"
    org_id: str = "default-org"

