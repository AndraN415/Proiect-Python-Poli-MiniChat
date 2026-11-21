from datetime import timedelta


SQLALCHEMY_DATABASE_URI: str = "sqlite:///../db.sqlite"
SECRET_KEY: str = "change_in_prod"
PERMANENT_SESSION_LIFETIME: timedelta = timedelta(days=7)
# request bodies are small JSON objects; anything bigger gets a 413
MAX_CONTENT_LENGTH: int = 16 * 1024
