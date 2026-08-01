
import app.models
from app.database import Base, engine


# this file resposible for loading Models and creating the database tables##
### ---- ONce we learn Alembic this file will be for testing not production env as we will use it in creating tables and manage DB migrations and versioning ---- ###
#### Alembic manage the DB Schema 


def init_db():
    Base.metadata.create_all(bind=engine)