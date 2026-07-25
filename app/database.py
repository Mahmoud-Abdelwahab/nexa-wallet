# 1. إنشاء Engine

# 2. إنشاء Session Factory

# 3. Dependency ترجع Session لكل Request

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase
from app.core.config import settings

class Base(DeclarativeBase):
      pass

# Engine (Singleton)
engine = create_engine(
    settings.database_url,
)


# Session Factory (Singleton)
#  bind session factory to the engine so session can request connections from the engine and engine get it from connection pool
#  why it named SessionLocal ? it just means that this session is linked to this request 
# and will be closed after the request is done, so it is local to the request and you can name it as you want but this is commonly used name
SessionLocal = sessionmaker(
    bind=engine
)
 
 # Dependency
 # SessionLocal is the session factory that will be used to create new Session objects for each request. It is configured to use the engine we created earlier, which connects to our database.
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()