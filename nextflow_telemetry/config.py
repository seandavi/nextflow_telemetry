import os
from dataclasses import dataclass

@dataclass
class Settings:
    SQLALCHEMY_URI: str

settings = Settings(
    SQLALCHEMY_URI=os.environ.get('SQLALCHEMY_URI', 'postgresql://postgres:postgres@localhost:5432/cmgd_dev')
)
