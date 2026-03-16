from pydantic import BaseModel, Field
from typing import Optional, Dict, List

class Question(BaseModel):
    id: str
    front_image: str
    back_image: str
    tags: List[str] = Field(default_factory=list)

