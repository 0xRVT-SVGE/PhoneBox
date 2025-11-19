# back_end/server/app.py
from flask import Flask
from flask_cors import CORS
from flask_socketio import SocketIO
from back_end.Database.API.students_API import students_bp
from back_end.Database.API.phones_API import phones_bp


def create_app():
    app = Flask(__name__)
    CORS(app)
    app.register_blueprint(students_bp, url_prefix="/api/students")
    app.register_blueprint(phones_bp, url_prefix="/api/phones")

    socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")


    return app, socketio
