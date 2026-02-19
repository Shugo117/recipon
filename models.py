from sqlalchemy import Column, Integer, String, DateTime, func
from database import Base

class RecipeLink(Base):
    __tablename__ = "recipe_links"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String(2048), nullable=False, unique=True, index=True)
    title = Column(String(512), nullable=False)
    category = Column(String(128), nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
