import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-insecure-key-CHANGE-IN-PRODUCTION'
    JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY') or 'dev-insecure-jwt-CHANGE-IN-PRODUCTION'
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///cleaning_service.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
