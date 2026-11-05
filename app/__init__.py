from flask import Flask
app=Flask(__name__)


app.config.from_pyfile("config.py")
app.config.from_prefixed_env()
from app.db import db



with app.app_context():
    db.create_all()
